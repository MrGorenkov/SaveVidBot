# main.py
import logging
import re
import os
import shutil
import asyncio
import yt_dlp
import json
from datetime import datetime
from typing import Optional, Tuple

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

# ----------------- ЛОГИ -----------------
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.DEBUG
)
logger = logging.getLogger(__name__)

# ----------------- FFMPEG (для Render/без root) -----------------
# imageio-ffmpeg приносит статический ffmpeg — укажем путь yt-dlp.
FFMPEG_PATH = None
try:
    import imageio_ffmpeg
    FFMPEG_PATH = imageio_ffmpeg.get_ffmpeg_exe()
    os.environ["FFMPEG_LOCATION"] = FFMPEG_PATH  # для сторонних библ.
    logger.info(f"FFmpeg resolved at: {FFMPEG_PATH}")
except Exception as e:
    logger.warning(f"FFmpeg not resolved from imageio-ffmpeg: {e}")

# ----------------- ТОКЕН -----------------
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not TOKEN:
    logger.error("TELEGRAM_BOT_TOKEN is not set in environment!")
    raise SystemExit("Set TELEGRAM_BOT_TOKEN env var before running.")

# ----------------- ФАЙЛ СТАТИСТИКИ -----------------
USER_STATS_FILE = 'user_stats.json'


def is_valid_url(url: str) -> bool:
    patterns = [
        # YouTube
        r'(?:https?://)?(?:www\.)?(?:youtube\.com/watch\?v=|youtu\.be/|youtube\.com/embed/|youtube\.com/v/|m\.youtube\.com/watch\?v=)[\w-]+',
        # TikTok
        r'(?:https?://)?(?:www\.)?(?:tiktok\.com/@[\w.-]+/(?:video|photo)/\d+|vm\.tiktok\.com/[\w-]+|m\.tiktok\.com/v/\d+)',
        # Instagram
        r'(?:https?://)?(?:www\.)?instagram\.com/(?:p|reel|tv)/[\w-]+',
        # Twitter/X
        r'(?:https?://)?(?:www\.)?(?:twitter\.com|x\.com)/\w+/status/\d+',
        # Facebook
        r'(?:https?://)?(?:www\.)?facebook\.com/.*?/videos/\d+',
        # Общий паттерн
        r'(?:https?://)?[\w.-]+\.[\w]{2,}(?:/[\w.-]*)*/?'
    ]
    for pattern in patterns:
        if re.match(pattern, url, re.IGNORECASE):
            return True
    return False


def build_ydl_opts_base() -> dict:
    """Базовые опции yt-dlp для нашего бота."""
    ua = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36'
    opts = {
        'format': 'best[height<=720][filesize<50M]/best[filesize<50M]/best',
        'outtmpl': 'downloaded_video.%(ext)s',
        'merge_output_format': 'mp4',
        'noplaylist': True,
        'max_filesize': 50 * 1024 * 1024,
        'socket_timeout': 60,
        'retries': 3,
        'quiet': True,
        'no_warnings': True,
        'user_agent': ua,
        'http_chunk_size': 10 * 1024 * 1024,  # 10MB
        'http_headers': {
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5',
            'Sec-Fetch-Mode': 'navigate',
        },
        'extractor_args': {
            # уменьшает шанс "Sign in to confirm you're not a bot"/age-gate
            'youtube': {'player_client': ['android', 'ios']},
            'tiktok': {'webpage_download_timeout': 30}
        },
        'logger': logger,
        'writeinfojson': False,
    }
    if FFMPEG_PATH:
        opts['ffmpeg_location'] = FFMPEG_PATH
    return opts


def _copy_cookiefile_to_tmp(src_path: str) -> str:
    """
    Делаем рабочую копию cookie-файла в /tmp:
    - Secret Files на Render read-only → никогда не пишем в /etc/secrets/*
    - добавляем заголовок Netscape при его отсутствии
    """
    dst_path = "/tmp/cookies_runtime.txt"
    with open(src_path, "rb") as s, open(dst_path, "wb") as d:
        data = s.read()
        if not data.startswith(b"# Netscape HTTP Cookie File"):
            d.write(b"# Netscape HTTP Cookie File\n")
        d.write(data)
    return dst_path


def _resolve_cookiefile() -> Optional[str]:
    """
    Возвращает путь к РАБОЧЕЙ копии cookies в /tmp (если исходный файл существует).
    Порядок: YTDLP_COOKIES_FILE → ./cookies.txt → None
    """
    src = os.getenv("YTDLP_COOKIES_FILE")
    if src and os.path.exists(src):
        try:
            return _copy_cookiefile_to_tmp(src)
        except Exception as e:
            logger.warning(f"Failed to copy env cookiefile to /tmp: {e}")
            return src  # fallback: хоть так, но не будем править оригинал

    local = os.path.join(os.getcwd(), "cookies.txt")
    if os.path.exists(local):
        try:
            return _copy_cookiefile_to_tmp(local)
        except Exception as e:
            logger.warning(f"Failed to copy local cookiefile to /tmp: {e}")
            return local

    return None


def get_ydl_opts_with_cookies(url: str) -> dict:
    """
    1) Пробуем cookie-файл (через /tmp-копию)
    2) (локально) cookies из браузера
    3) Фолбэк без куки
    """
    base = build_ydl_opts_base()

    # 1) cookie-файл
    cf = _resolve_cookiefile()
    if cf and os.path.exists(cf):
        logger.info(f"Using cookies file: {cf}")
        base['cookiefile'] = cf
        return base

    # 2) Попытка взять куки из установленного браузера (на Render профилей обычно нет)
    browsers_to_try = [
        ('firefox', None, None, None),
        ('chrome', None, None, None),
        ('safari', None, None, None),
    ]
    for b in browsers_to_try:
        try:
            test_opts = build_ydl_opts_base()
            test_opts['cookiesfrombrowser'] = b
            with yt_dlp.YoutubeDL(test_opts) as ydl:
                ydl.extract_info(url, download=False)
                logger.info(f"Successfully using {b[0]} cookies")
                return test_opts
        except Exception as e:
            logger.debug(f"No browser cookies from {b[0]}: {e}")

    logger.info("Proceeding without cookies (may fail on YouTube).")
    return base


async def download_video(url: str) -> Tuple[Optional[Tuple[str, str]], Optional[str]]:
    try:
        loop = asyncio.get_running_loop()

        def sync_download():
            ydl_opts = get_ydl_opts_with_cookies(url)

            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                logger.debug(f"Starting download for URL: {url}")

                try:
                    info = ydl.extract_info(url, download=False)
                    if info.get('entries'):
                        info = info['entries'][0]
                    logger.debug(f"Video info extracted: {info.get('title', 'Unknown')}")
                except Exception as e:
                    logger.error(f"Failed to extract info: {e}")
                    raise

                filesize = info.get('filesize') or info.get('filesize_approx')
                if filesize and filesize > 50 * 1024 * 1024:
                    raise Exception("Видео превышает максимальный размер (50 МБ)")

                # Скачивание
                info = ydl.extract_info(url, download=True)
                if info.get('entries'):
                    info = info['entries'][0]

                filename = ydl.prepare_filename(info)

                if not os.path.exists(filename):
                    base_name = os.path.splitext(filename)[0]
                    for ext in ['.mp4', '.webm', '.mkv', '.avi']:
                        test_filename = base_name + ext
                        if os.path.exists(test_filename):
                            filename = test_filename
                            break

                # Переименуем в .mp4 (без перекодирования), если нужно
                if not filename.endswith('.mp4'):
                    base_name, _ = os.path.splitext(filename)
                    new_filename = f"{base_name}.mp4"
                    try:
                        if os.path.exists(filename):
                            os.rename(filename, new_filename)
                        elif os.path.exists(new_filename):
                            pass
                        filename = new_filename
                    except Exception:
                        # Если переименовать нельзя — оставим исходное имя
                        pass

                logger.debug(
                    f"Downloaded file: {filename}, size: {os.path.getsize(filename) if os.path.exists(filename) else 'N/A'} bytes")
                return filename, info.get('title', 'video')

        result = await loop.run_in_executor(None, sync_download)
        return result, None

    except yt_dlp.utils.MaxDownloadsReached:
        logger.error("File exceeds max size")
        return None, "Видео превышает максимальный размер (50 МБ)."
    except yt_dlp.utils.UnsupportedError as e:
        logger.error(f"Unsupported URL or format: {e}")
        return None, "Неподдерживаемая платформа или формат видео."
    except yt_dlp.utils.ExtractorError as e:
        logger.error(f"Extractor error: {e}")
        msg = str(e)
        if "The provided YouTube account cookies are no longer valid" in msg:
            return None, "Куки YouTube устарели — экспортируй новые (cookies.txt) и перезалей."
        if "Sign in to confirm you’re not a bot" in msg or "Sign in to confirm you're not a bot" in msg:
            return None, "YouTube требует авторизацию (cookies.txt)."
        if "Requested format is not available" in msg:
            return None, "Запрашиваемый формат недоступен. Попробуй другое видео."
        return None, f"Ошибка извлечения видео: {msg}"
    except Exception as e:
        logger.error(f"Error downloading video: {e}")
        msg = str(e)
        if "Video unavailable" in msg:
            return None, "Видео недоступно или удалено."
        elif "Private video" in msg:
            return None, "Видео является приватным."
        elif "Sign in to confirm your age" in msg:
            return None, "Видео имеет возрастные ограничения (нужен cookies.txt)."
        return None, f"Ошибка скачивания: {msg}"


def load_user_stats():
    try:
        if os.path.exists(USER_STATS_FILE):
            with open(USER_STATS_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
    except Exception as e:
        logger.error(f"Error loading user stats: {e}")
    return {}


def save_user_stats(stats):
    try:
        with open(USER_STATS_FILE, 'w', encoding='utf-8') as f:
            json.dump(stats, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Error saving user stats: {e}")


def update_user_stats(user_id: int, platform: str, file_size: int = 0):
    stats = load_user_stats()
    user_id_str = str(user_id)

    if user_id_str not in stats:
        stats[user_id_str] = {
            'downloads_count': 0,
            'total_size': 0,
            'platforms': {},
            'first_use': datetime.now().isoformat(),
            'last_activity': datetime.now().isoformat()
        }

    stats[user_id_str]['downloads_count'] += 1
    stats[user_id_str]['total_size'] += file_size
    stats[user_id_str]['last_activity'] = datetime.now().isoformat()

    if platform in stats[user_id_str]['platforms']:
        stats[user_id_str]['platforms'][platform] += 1
    else:
        stats[user_id_str]['platforms'][platform] = 1

    save_user_stats(stats)


def get_user_stats(user_id: int):
    stats = load_user_stats()
    return stats.get(str(user_id), None)


def detect_platform(url: str) -> str:
    if 'youtube.com' in url or 'youtu.be' in url:
        return 'YouTube'
    elif 'tiktok.com' in url:
        return 'TikTok'
    elif 'instagram.com' in url:
        return 'Instagram'
    elif 'twitter.com' in url or 'x.com' in url:
        return 'Twitter/X'
    elif 'facebook.com' in url:
        return 'Facebook'
    else:
        return 'Другое'


def format_file_size(size_bytes: int) -> str:
    if size_bytes == 0:
        return "0 Б"
    size_names = ["Б", "КБ", "МБ", "ГБ", "ТБ"]
    i = 0
    size = float(size_bytes)
    while size >= 1024.0 and i < len(size_names) - 1:
        size /= 1024.0
        i += 1
    return f"{size:.1f} {size_names[i]}"


# ----------------- AIROGRAM -----------------
bot = Bot(token=TOKEN)
dp = Dispatcher()


@dp.message(CommandStart())
async def start(message: types.Message):
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Моя статистика", callback_data="show_stats")]
    ])
    await message.reply(
        "🎥 Привет! Я раб Санька — бот для скачивания видео!\n\n"
        "📱 Поддерживаемые платформы:\n"
        "• YouTube\n"
        "• TikTok\n"
        "• Instagram\n"
        "• Twitter/X\n"
        "• Facebook\n"
        "• И другие!\n\n"
        "📤 Отправь ссылку на видео — я его скачаю.\n"
        "⚠️ Максимальный размер файла: 50 МБ",
        reply_markup=keyboard
    )


@dp.callback_query(F.data == "show_stats")
async def show_stats_callback(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    await callback.answer()
    stats = get_user_stats(user_id)

    if not stats:
        await callback.message.answer("📊 У тебя пока нет статистики. Скачай первое видео!")
        return

    first_use = datetime.fromisoformat(stats['first_use']).strftime("%d.%m.%Y")
    last_activity = datetime.fromisoformat(stats['last_activity']).strftime("%d.%m.%Y %H:%M")

    if stats['platforms']:
        favorite_platform = max(stats['platforms'], key=stats['platforms'].get)
        favorite_count = stats['platforms'][favorite_platform]
    else:
        favorite_platform = "Нет данных"
        favorite_count = 0

    stats_text = (
        f"📊 **Твоя статистика:**\n\n"
        f"📥 Скачано видео: **{stats['downloads_count']}**\n"
        f"💾 Общий размер: **{format_file_size(stats['total_size'])}**\n"
        f"🏆 Любимая платформа: **{favorite_platform}** ({favorite_count} видео)\n"
        f"📅 Первое использование: **{first_use}**\n"
        f"🕐 Последняя активность: **{last_activity}**\n\n"
        f"🎯 **По платформам:**\n"
    )
    for platform, count in stats['platforms'].items():
        stats_text += f"• {platform}: {count} видео\n"

    try:
        await callback.message.edit_text(stats_text, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Error editing stats message: {e}")
        await callback.message.answer(stats_text, parse_mode="Markdown")


@dp.message()
async def handle_message(message: types.Message):
    user_id = message.from_user.id
    url = (message.text or "").strip()

    if not is_valid_url(url):
        await message.reply(
            "❌ Пожалуйста, отправь корректную ссылку на видео.\n\n"
            "Примеры:\n"
            "• https://www.youtube.com/watch?v=...\n"
            "• https://www.tiktok.com/@user/video/...\n"
            "• https://www.instagram.com/p/...\n"
            "• https://twitter.com/user/status/..."
        )
        return

    processing_msg = await message.reply("⏳ Обрабатываю ссылку, скачиваю видео...")

    result, error_msg = await download_video(url)
    if result:
        filename, title = result
        try:
            if not os.path.exists(filename):
                logger.error(f"File {filename} does not exist")
                await processing_msg.edit_text("❌ Ошибка: скачанный файл не найден. Попробуй другую ссылку.")
                return

            file_size = os.path.getsize(filename)
            logger.debug(f"File size: {file_size} bytes")

            if file_size > 50 * 1024 * 1024:
                logger.error(f"File too large: {file_size} bytes")
                await processing_msg.edit_text(
                    "❌ Видео слишком большое для Telegram (макс. 50 МБ). Попробуй другое видео."
                )
                os.remove(filename)
                return

            await processing_msg.edit_text("📤 Отправляю видео...")

            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📊 Моя статистика", callback_data="show_stats")]
            ])

            await message.reply_video(
                video=types.FSInputFile(filename),
                caption=f"✅ Скачано: {title}",
                supports_streaming=True,
                reply_markup=keyboard
            )

            platform = detect_platform(url)
            update_user_stats(user_id, platform, file_size)

            logger.debug(f"Video sent successfully: {filename}")
            os.remove(filename)
            await processing_msg.delete()

        except Exception as e:
            logger.error(f"Error sending video: {e}")
            await processing_msg.edit_text(f"❌ Не удалось отправить видео: {e}. Попробуй другую ссылку.")
            if os.path.exists(filename):
                os.remove(filename)
    else:
        hint = ""
        if error_msg and "cookies" in error_msg.lower():
            hint = "\n\n💡 Решение: загрузите свежий cookies.txt (Netscape) и укажите YTDLP_COOKIES_FILE."
        reply_text = (
            "❌ Не удалось скачать видео. Проверь ссылку или попробуй позже.\n"
            "💡 Часто помогает dev-версия yt-dlp и свежие куки."
            f"{hint}"
        )
        if error_msg:
            reply_text += f"\n\n🔍 Детали ошибки: {error_msg}"
        await processing_msg.edit_text(reply_text)


async def main():
    logger.info("Бот запущен")
    await dp.start_polling(bot)


if __name__ == '__main__':
    asyncio.run(main())
