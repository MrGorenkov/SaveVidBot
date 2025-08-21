# main.py
import logging
import re
import os
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

# ----------------- FFMPEG -----------------
FFMPEG_PATH = None
try:
    import imageio_ffmpeg
    FFMPEG_PATH = imageio_ffmpeg.get_ffmpeg_exe()
    os.environ["FFMPEG_LOCATION"] = FFMPEG_PATH
    logger.info(f"FFmpeg resolved at: {FFMPEG_PATH}")
except Exception as e:
    logger.warning(f"FFmpeg not resolved from imageio-ffmpeg: {e}")

# ----------------- ТОКЕН БОТА -----------------
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not TOKEN:
    logger.error("TELEGRAM_BOT_TOKEN is not set in environment!")
    raise SystemExit("Set TELEGRAM_BOT_TOKEN env var before running.")

# ----------------- ОКРУЖЕНИЕ YT-DLP -----------------
YTDLP_COOKIES_FILE = os.getenv("YTDLP_COOKIES_FILE")  # напр. /etc/secrets/cookies.txt
YTDLP_PO_TOKENS = os.getenv("YTDLP_PO_TOKENS", "").strip()  # напр. mweb.gvs+XXXXX[,mweb.player+YYYY]
YTDLP_PLAYER_CLIENTS = os.getenv("YTDLP_PLAYER_CLIENTS", "").strip()  # кастом, если нужно
YTDLP_PROXY = os.getenv("YTDLP_PROXY", "").strip()  # напр. http://user:pass@host:port

USER_STATS_FILE = 'user_stats.json'

# ----------------- УТИЛИТЫ -----------------
def is_valid_url(url: str) -> bool:
    patterns = [
        r'(?:https?://)?(?:www\.)?(?:youtube\.com/watch\?v=|youtu\.be/|youtube\.com/embed/|youtube\.com/v/|m\.youtube\.com/watch\?v=)[\w-]+',
        r'(?:https?://)?(?:www\.)?(?:tiktok\.com/@[\w.-]+/(?:video|photo)/\d+|vm\.tiktok\.com/[\w-]+|m\.tiktok\.com/v/\d+)',
        r'(?:https?://)?(?:www\.)?instagram\.com/(?:p|reel|tv)/[\w-]+',
        r'(?:https?://)?(?:www\.)?(?:twitter\.com|x\.com)/\w+/status/\d+',
        r'(?:https?://)?(?:www\.)?facebook\.com/.*?/videos/\d+',
        r'(?:https?://)?[\w.-]+\.[\w]{2,}(?:/[\w.-]*)*/?'
    ]
    for pattern in patterns:
        if re.match(pattern, url, re.IGNORECASE):
            return True
    return False


def _copy_cookiefile_to_tmp(src_path: str) -> str:
    """Создаёт рабочую копию cookie-файла в /tmp, добавляет заголовок Netscape при его отсутствии."""
    dst_path = "/tmp/cookies_runtime.txt"
    with open(src_path, "rb") as s, open(dst_path, "wb") as d:
        data = s.read()
        if not data.startswith(b"# Netscape HTTP Cookie File"):
            d.write(b"# Netscape HTTP Cookie File\n")
        d.write(data)
    return dst_path


def _resolve_cookiefile() -> Optional[str]:
    """Возвращает путь к РАБОЧЕЙ копии cookies в /tmp, если исходный файл существует."""
    if YTDLP_COOKIES_FILE and os.path.exists(YTDLP_COOKIES_FILE):
        try:
            path = _copy_cookiefile_to_tmp(YTDLP_COOKIES_FILE)
            logger.info(f"Using cookies file: {path}")
            return path
        except Exception as e:
            logger.warning(f"Failed to copy env cookiefile: {e}")
            return YTDLP_COOKIES_FILE

    local = os.path.join(os.getcwd(), "cookies.txt")
    if os.path.exists(local):
        try:
            path = _copy_cookiefile_to_tmp(local)
            logger.info(f"Using local cookies file: {path}")
            return path
        except Exception as e:
            logger.warning(f"Failed to copy local cookiefile: {e}")
            return local
    return None


def build_ydl_opts_base(po_tokens: str) -> dict:
    """Базовые опции yt-dlp + учёт PO-токенов/прокси/клиента."""
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
        'http_chunk_size': 10 * 1024 * 1024,
        'logger': logger,
        'writeinfojson': False,
    }
    if FFMPEG_PATH:
        opts['ffmpeg_location'] = FFMPEG_PATH
    if YTDLP_PROXY:
        # Прямо укажем прокси yt-dlp (параллельно можно оставить HTTP(S)_PROXY в env)
        opts['proxy'] = YTDLP_PROXY

    # --- extractor_args: выбор клиента и PO-токены ---
    # Если задано YTDLP_PLAYER_CLIENTS, берём его; иначе:
    # - при наличии PO токенов рекомендуем mweb; без токенов — android/ios.
    if YTDLP_PLAYER_CLIENTS:
        players = [p.strip() for p in YTDLP_PLAYER_CLIENTS.split(",") if p.strip()]
    elif po_tokens:
        players = ['default', 'mweb']  # рекомендовано wiki при использовании PO токенов
    else:
        players = ['android', 'ios']   # попытка обойти по мобилам без PO

    extractor_args = {
        'youtube': {
            'player_client': players,
        },
        'tiktok': {
            'webpage_download_timeout': 30
        }
    }
    if po_tokens:
        # Формат: CLIENT.CONTEXT+TOKEN, через запятую. Пример: mweb.gvs+AAA,mweb.player+BBB
        extractor_args['youtube']['po_token'] = po_tokens  # yt-dlp принимает строку со списком, см. manpage

    opts['extractor_args'] = extractor_args
    return opts


def get_ydl_opts(url: str) -> dict:
    """Собираем финальные опции yt-dlp: куки (+mweb/PO), прокси."""
    opts = build_ydl_opts_base(YTDLP_PO_TOKENS)
    cf = _resolve_cookiefile()
    if cf and os.path.exists(cf):
        opts['cookiefile'] = cf
    return opts


async def download_video(url: str) -> Tuple[Optional[Tuple[str, str]], Optional[str]]:
    try:
        loop = asyncio.get_running_loop()

        def sync_download():
            ydl_opts = get_ydl_opts(url)
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

                fs = info.get('filesize') or info.get('filesize_approx')
                if fs and fs > 50 * 1024 * 1024:
                    raise Exception("Видео превышает максимальный размер (50 МБ)")

                info = ydl.extract_info(url, download=True)
                if info.get('entries'):
                    info = info['entries'][0]

                filename = ydl.prepare_filename(info)
                if not os.path.exists(filename):
                    base = os.path.splitext(filename)[0]
                    for ext in ['.mp4', '.webm', '.mkv', '.avi']:
                        test = base + ext
                        if os.path.exists(test):
                            filename = test
                            break
                if not filename.endswith('.mp4'):
                    base, _ = os.path.splitext(filename)
                    new_filename = f"{base}.mp4"
                    try:
                        if os.path.exists(filename):
                            os.rename(filename, new_filename)
                        filename = new_filename if os.path.exists(new_filename) else filename
                    except Exception:
                        pass

                return filename, info.get('title', 'video')

        result = await loop.run_in_executor(None, sync_download)
        return result, None

    except yt_dlp.utils.MaxDownloadsReached:
        return None, "Видео превышает максимальный размер (50 МБ)."
    except yt_dlp.utils.UnsupportedError as e:
        return None, "Неподдерживаемая платформа или формат видео."
    except yt_dlp.utils.ExtractorError as e:
        msg = str(e)
        if "The provided YouTube account cookies are no longer valid" in msg:
            return None, "Куки YouTube устарели — переэкспортируй cookies.txt и перезагрузи сервис."
        if "Sign in to confirm you’re not a bot" in msg or "Sign in to confirm you're not a bot" in msg:
            return None, "YouTube требует авторизацию/PO-токен. Добавь свежий cookies.txt и/или YTDLP_PO_TOKENS."
        if "Requested format is not available" in msg:
            return None, "Запрашиваемый формат недоступен. Попробуй другое видео."
        return None, f"Ошибка извлечения видео: {msg}"
    except Exception as e:
        msg = str(e)
        if "Video unavailable" in msg:
            return None, "Видео недоступно или удалено."
        elif "Private video" in msg:
            return None, "Видео приватное."
        elif "Sign in to confirm your age" in msg:
            return None, "Возрастное ограничение — нужен cookies.txt."
        return None, f"Ошибка скачивания: {msg}"


# ----------------- СТАТИСТИКА -----------------
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
    uid = str(user_id)
    if uid not in stats:
        stats[uid] = {
            'downloads_count': 0,
            'total_size': 0,
            'platforms': {},
            'first_use': datetime.now().isoformat(),
            'last_activity': datetime.now().isoformat()
        }
    stats[uid]['downloads_count'] += 1
    stats[uid]['total_size'] += file_size
    stats[uid]['last_activity'] = datetime.now().isoformat()
    stats[uid]['platforms'][platform] = stats[uid]['platforms'].get(platform, 0) + 1
    save_user_stats(stats)


def get_user_stats(user_id: int):
    return load_user_stats().get(str(user_id), None)


def detect_platform(url: str) -> str:
    if 'youtube.com' in url or 'youtu.be' in url:
        return 'YouTube'
    if 'tiktok.com' in url:
        return 'TikTok'
    if 'instagram.com' in url:
        return 'Instagram'
    if 'twitter.com' in url or 'x.com' in url:
        return 'Twitter/X'
    if 'facebook.com' in url:
        return 'Facebook'
    return 'Другое'


def format_file_size(size_bytes: int) -> str:
    if size_bytes == 0:
        return "0 Б"
    units = ["Б", "КБ", "МБ", "ГБ", "ТБ"]
    i = 0
    size = float(size_bytes)
    while size >= 1024.0 and i < len(units) - 1:
        size /= 1024.0
        i += 1
    return f"{size:.1f} {units[i]}"


# ----------------- AIROGRAM -----------------
bot = Bot(token=TOKEN)
dp = Dispatcher()

@dp.message(CommandStart())
async def start(message: types.Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Моя статистика", callback_data="show_stats")]
    ])
    await message.reply(
        "🎥 Привет! Я раб Санька — бот для скачивания видео.\n\n"
        "Поддерживаю: YouTube, TikTok, Instagram, Twitter/X, Facebook и др.\n"
        "Отправь ссылку — скачаю до 50 МБ.\n\n"
        "Если YouTube просит «не бот» — добавь cookies.txt и/или YTDLP_PO_TOKENS.",
        reply_markup=kb
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
        fav = max(stats['platforms'], key=stats['platforms'].get)
        fav_count = stats['platforms'][fav]
    else:
        fav, fav_count = "Нет данных", 0
    text = (
        f"📊 **Твоя статистика:**\n\n"
        f"📥 Скачано видео: **{stats['downloads_count']}**\n"
        f"💾 Общий размер: **{format_file_size(stats['total_size'])}**\n"
        f"🏆 Любимая платформа: **{fav}** ({fav_count})\n"
        f"📅 Первое использование: **{first_use}**\n"
        f"🕐 Последняя активность: **{last_activity}**\n\n"
        f"🎯 **По платформам:**\n"
    )
    for platform, count in stats['platforms'].items():
        text += f"• {platform}: {count}\n"
    try:
        await callback.message.edit_text(text, parse_mode="Markdown")
    except Exception:
        await callback.message.answer(text, parse_mode="Markdown")

@dp.message()
async def handle_message(message: types.Message):
    user_id = message.from_user.id
    url = (message.text or "").strip()

    if not is_valid_url(url):
        await message.reply(
            "❌ Пришли корректную ссылку на видео.\n"
            "Примеры:\n"
            "• https://www.youtube.com/watch?v=...\n"
            "• https://www.tiktok.com/@user/video/...\n"
            "• https://www.instagram.com/p/...\n"
            "• https://twitter.com/user/status/..."
        )
        return

    processing = await message.reply("⏳ Обрабатываю ссылку, скачиваю видео...")

    result, error_msg = await download_video(url)
    if result:
        filename, title = result
        try:
            if not os.path.exists(filename):
                await processing.edit_text("❌ Ошибка: файл не найден. Попробуй другую ссылку.")
                return
            size = os.path.getsize(filename)
            if size > 50 * 1024 * 1024:
                await processing.edit_text("❌ Видео слишком большое для Telegram (макс. 50 МБ).")
                os.remove(filename)
                return

            await processing.edit_text("📤 Отправляю видео...")
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📊 Моя статистика", callback_data="show_stats")]
            ])
            await message.reply_video(
                video=types.FSInputFile(filename),
                caption=f"✅ Скачано: {title}",
                supports_streaming=True,
                reply_markup=kb
            )
            update_user_stats(user_id, detect_platform(url), size)
            os.remove(filename)
            await processing.delete()
        except Exception as e:
            await processing.edit_text(f"❌ Не удалось отправить видео: {e}. Попробуй другую ссылку.")
            if os.path.exists(filename):
                os.remove(filename)
    else:
        hint = ""
        if error_msg and ("cookies" in error_msg.lower() or "po" in error_msg.lower()):
            hint = "\n\n💡 Проверь cookies.txt (Netscape) и/или добавь YTDLP_PO_TOKENS."
        await processing.edit_text(
            "❌ Не удалось скачать видео. Попробуй позже или другую ссылку."
            f"{hint}\n\n🔍 Детали ошибки: {error_msg or 'нет'}"
        )

async def main():
    logger.info("Бот запущен")
    await dp.start_polling(bot)

if __name__ == '__main__':
    asyncio.run(main())
