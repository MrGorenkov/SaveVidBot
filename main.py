import logging
import re
import os
import sys
import asyncio
import json
from datetime import datetime
from typing import Optional, Tuple

import yt_dlp
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

# =========================
# НАСТРОЙКИ / ENV
# =========================
# 1) Telegram токен:
#    - Рекомендуется задать ENV: TELEGRAM_BOT_TOKEN
#    - Либо вписать сюда в константу TOKEN_FALLBACK
TOKEN_FALLBACK = ""  # <-- можешь вписать токен сюда, если не хочешь через ENV

# 2) Путь к cookies (Netscape):
#    - По умолчанию ищем: ENV COOKIES_PATH -> /etc/secrets/cookies.txt -> /mnt/data/cookies.txt
#    - Файл ДОЛЖЕН начинаться строкой "# Netscape HTTP Cookie File" или "# HTTP Cookie File"
DEFAULT_COOKIES_CANDIDATES = [
    "/etc/secrets/cookies.txt",
    "/mnt/data/cookies.txt",
]

# 3) YouTube poToken (web):
#    - ENV: PO_TOKEN_WEB (значение БЕЗ префикса "web+")
#    - Токен берется через HAR/Network или скриптом из браузера
#    - При наличии будет добавлен как "web+<токен>"
PO_TOKEN_ENV_NAME = "PO_TOKEN_WEB"

# 4) User-Agent (желательно тот же браузера, из которого брали poToken/cookies)
DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.6 Safari/605.1.15"
)


# =========================
# ЛОГИРОВАНИЕ
# =========================
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.DEBUG,
)
logger = logging.getLogger(__name__)

# =========================
# ФАЙЛ ДЛЯ СТАТИСТИКИ
# =========================
USER_STATS_FILE = "user_stats.json"


# =========================
# УТИЛИТЫ
# =========================
def get_telegram_token() -> str:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if token:
        return token
    if TOKEN_FALLBACK.strip():
        return TOKEN_FALLBACK.strip()
    logger.error("TELEGRAM_BOT_TOKEN is not set and TOKEN_FALLBACK is empty!")
    sys.exit(1)


def find_cookiefile() -> Optional[str]:
    """Возвращает путь к валидному Netscape cookies.txt или None."""
    # явный путь из ENV
    env_path = os.getenv("COOKIES_PATH", "").strip()
    candidates = []
    if env_path:
        candidates.append(env_path)
    candidates.extend(DEFAULT_COOKIES_CANDIDATES)

    for p in candidates:
        if p and os.path.isfile(p):
            try:
                with open(p, "r", encoding="utf-8", errors="ignore") as f:
                    first = f.readline().strip()
                if first.startswith("# Netscape HTTP Cookie File") or first.startswith("# HTTP Cookie File"):
                    logger.info(f"Using cookies file: {p}")
                    return p
                else:
                    logger.warning(f"Cookies file found but not Netscape format: {p}")
            except Exception as e:
                logger.warning(f"Cannot read cookies file {p}: {e}")
    logger.warning("No valid Netscape cookies file found. Proceeding without cookies.")
    return None


def is_valid_url(url: str) -> bool:
    patterns = [
        r'(?:https?://)?(?:www\.)?(?:youtube\.com/watch\?v=|youtu\.be/|youtube\.com/embed/|youtube\.com/v/|m\.youtube\.com/watch\?v=)[\w-]+',
        r'(?:https?://)?(?:www\.)?(?:tiktok\.com/@[\w.-]+/video/\d+|vm\.tiktok\.com/[\w-]+|m\.tiktok\.com/v/\d+)',
        r'(?:https?://)?(?:www\.)?instagram\.com/(?:p|reel|tv)/[\w-]+',
        r'(?:https?://)?(?:www\.)?(?:twitter\.com|x\.com)/\w+/status/\d+',
        r'(?:https?://)?(?:www\.)?facebook\.com/.*?/videos/\d+',
        r'(?:https?://)?[\w.-]+\.[\w]{2,}(?:/[\w.-]*)*/?',
    ]
    for pattern in patterns:
        if re.match(pattern, url, re.IGNORECASE):
            return True
    return False


def load_user_stats():
    try:
        if os.path.exists(USER_STATS_FILE):
            with open(USER_STATS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        logger.error(f"Error loading user stats: {e}")
    return {}


def save_user_stats(stats):
    try:
        with open(USER_STATS_FILE, "w", encoding="utf-8") as f:
            json.dump(stats, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Error saving user stats: {e}")


def update_user_stats(user_id: int, platform: str, file_size: int = 0):
    stats = load_user_stats()
    user_id_str = str(user_id)

    if user_id_str not in stats:
        stats[user_id_str] = {
            "downloads_count": 0,
            "total_size": 0,
            "platforms": {},
            "first_use": datetime.now().isoformat(),
            "last_activity": datetime.now().isoformat(),
        }

    stats[user_id_str]["downloads_count"] += 1
    stats[user_id_str]["total_size"] += file_size
    stats[user_id_str]["last_activity"] = datetime.now().isoformat()

    stats[user_id_str]["platforms"][platform] = stats[user_id_str]["platforms"].get(platform, 0) + 1

    save_user_stats(stats)


def get_user_stats(user_id: int):
    stats = load_user_stats()
    return stats.get(str(user_id), None)


def detect_platform(url: str) -> str:
    if "youtube.com" in url or "youtu.be" in url:
        return "YouTube"
    if "tiktok.com" in url:
        return "TikTok"
    if "instagram.com" in url:
        return "Instagram"
    if "twitter.com" in url or "x.com" in url:
        return "Twitter/X"
    if "facebook.com" in url:
        return "Facebook"
    return "Другое"


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


# =========================
# СКАЧИВАНИЕ ВИДЕО
# =========================
async def download_video(url: str) -> Tuple[Optional[Tuple[str, str]], Optional[str]]:
    # poToken (web) из ENV, добавим префикс 'web+'
    po_token_raw = os.getenv(PO_TOKEN_ENV_NAME, "").strip()
    po_token_full = f"web+{po_token_raw}" if po_token_raw else ""

    # User-Agent из ENV или дефолт
    YTDLP_UA = os.getenv("YTDLP_UA", DEFAULT_UA).strip()

    # Cookies (Netscape)
    cookiefile = find_cookiefile()

    ydl_opts = {
        "format": "best[height<=720][filesize<50M]/best[filesize<50M]/best",
        "outtmpl": "downloaded_video.%(ext)s",
        "merge_output_format": "mp4",
        "quiet": False,
        "no_warnings": False,
        "verbose": True,
        "noplaylist": True,
        "max_filesize": 50 * 1024 * 1024,
        "http_chunk_size": 10 * 1024 * 1024,  # <= 10MB — анти-throttle на YouTube
        "user_agent": YTDLP_UA,
        "logger": logger,
        "socket_timeout": 15,
        "extractor_retries": 2,
        "retries": 1,
        "concurrent_fragment_downloads": 1,
        "http_headers": {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "ru,en;q=0.5",
            "Sec-Fetch-Mode": "navigate",
        },
        "extractor_args": {
            "tiktok": {"webpage_download_timeout": 30},
            "youtube": {
                # просим web-клиент (совместим с poToken)
                "player_client": ["web", "default"],
                **({"po_token": po_token_full} if po_token_full else {}),
            },
            # для вкладок/плейлистов (на всякий)
            "youtubetab": ({ "po_token": po_token_full } if po_token_full else {}),
        },
    }

    if cookiefile:
        ydl_opts["cookiefile"] = cookiefile

    try:
        loop = asyncio.get_running_loop()

        def sync_download():
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                logger.debug(f"Starting download for URL: {url}")

                # 1) Сначала пробуем достать info БЕЗ скачивания — оценим размер
                try:
                    info = ydl.extract_info(url, download=False)
                    logger.debug(f"Video info extracted: {info.get('title', 'Unknown')}")
                except Exception as e:
                    logger.error(f"Failed to extract info: {e}")
                    raise

                # Размер может быть в разных полях, но часто None — проверим только если есть
                filesize = info.get("filesize") or info.get("filesize_approx")
                if filesize and filesize > 50 * 1024 * 1024:
                    raise Exception("Видео превышает максимальный размер (50 МБ)")

                # 2) Скачиваем
                info = ydl.extract_info(url, download=True)
                filename = ydl.prepare_filename(info)

                # Попробуем найти реальный файл (иногда расширение отличается)
                if not os.path.exists(filename):
                    base = os.path.splitext(filename)[0]
                    for ext in [".mp4", ".webm", ".mkv", ".avi"]:
                        test = base + ext
                        if os.path.exists(test):
                            filename = test
                            break

                # Приведем к .mp4, если возможно
                if not filename.endswith(".mp4"):
                    base, ext = os.path.splitext(filename)
                    new_filename = f"{base}.mp4"
                    if os.path.exists(filename):
                        try:
                            os.rename(filename, new_filename)
                            filename = new_filename
                        except Exception:
                            # если переименовать нельзя — оставляем как есть
                            pass
                    elif os.path.exists(new_filename):
                        filename = new_filename

                size_after = os.path.getsize(filename) if os.path.exists(filename) else 0
                logger.debug(f"Downloaded file: {filename}, size: {size_after} bytes")

                return filename, info.get("title", "video")

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
        if "Sign in to confirm you’re not a bot" in msg or "Sign in to confirm you're not a bot" in msg:
            hint = "Проверь cookies.txt (Netscape) и poToken (PO_TOKEN_WEB). Обнови их из того же браузера/профиля."
            return None, f"Требуется вход (anti-bot). {hint}"
        if "Requested format is not available" in msg:
            return None, "Запрашиваемый формат недоступен. Попробуй другое видео."
        if "The provided YouTube account cookies are no longer valid" in msg:
            return None, "Куки протухли. Экспортируй свежие cookies и замени файл."
        return None, f"Ошибка извлечения видео: {msg}"

    except Exception as e:
        logger.error(f"Error downloading video: {e}")
        error_msg = str(e)
        if "Video unavailable" in error_msg:
            return None, "Видео недоступно или удалено."
        if "Private video" in error_msg:
            return None, "Видео является приватным."
        if "age" in error_msg.lower():
            return None, "Видео имеет возрастные ограничения."
        return None, f"Ошибка скачивания: {error_msg}"


# =========================
# TELEGRAM БОТ
# =========================
bot = Bot(token=get_telegram_token())
dp = Dispatcher()


@dp.message(CommandStart())
async def start(message: types.Message):
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="📊 Моя статистика", callback_data="show_stats")]]
    )
    await message.reply(
        "🎥 Привет! Я раб Санька — бот для скачивания видео!\n\n"
        "📱 Поддерживаемые платформы:\n"
        "• YouTube • TikTok • Instagram • Twitter/X • Facebook • и др.\n\n"
        "📤 Пришли ссылку на видео — я скачаю и пришлю файл.\n"
        "⚠️ Максимальный размер файла: 50 МБ.",
        reply_markup=keyboard,
    )


@dp.callback_query(F.data == "show_stats")
async def show_stats_callback(callback: types.CallbackQuery):
    await callback.answer()
    user_id = callback.from_user.id
    stats = get_user_stats(user_id)

    if not stats:
        await callback.message.answer("📊 У тебя пока нет статистики. Скачай первое видео!")
        return

    first_use = datetime.fromisoformat(stats["first_use"]).strftime("%d.%m.%Y")
    last_activity = datetime.fromisoformat(stats["last_activity"]).strftime("%d.%m.%Y %H:%M")

    if stats["platforms"]:
        favorite_platform = max(stats["platforms"], key=stats["platforms"].get)
        favorite_count = stats["platforms"][favorite_platform]
    else:
        favorite_platform, favorite_count = "Нет данных", 0

    stats_text = (
        f"📊 **Твоя статистика:**\n\n"
        f"📥 Скачано видео: **{stats['downloads_count']}**\n"
        f"💾 Общий размер: **{format_file_size(stats['total_size'])}**\n"
        f"🏆 Любимая платформа: **{favorite_platform}** ({favorite_count} видео)\n"
        f"📅 Первое использование: **{first_use}**\n"
        f"🕐 Последняя активность: **{last_activity}**\n\n"
        f"🎯 **По платформам:**\n"
    )

    for platform, count in stats["platforms"].items():
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
                logger.error(f"File {filename} does not exist after download")
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

            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="📊 Моя статистика", callback_data="show_stats")]]
            )

            await message.reply_video(
                video=types.FSInputFile(filename),
                caption=f"✅ Скачано: {title}",
                supports_streaming=True,
                reply_markup=keyboard,
            )

            # Обновляем статистику
            platform = detect_platform(url)
            update_user_stats(user_id, platform, file_size)

            logger.debug(f"Video sent successfully: {filename}")
            os.remove(filename)
            await processing_msg.delete()

        except Exception as e:
            logger.error(f"Error sending video: {e}")
            await processing_msg.edit_text(f"❌ Не удалось отправить видео: {str(e)}. Попробуй другую ссылку.")
            if os.path.exists(filename):
                try:
                    os.remove(filename)
                except Exception:
                    pass
    else:
        # дружелюбный вывод + подсказка про poToken/cookies
        base = "❌ Не удалось скачать видео. Попробуй позже или другую ссылку."
        hint = "\n\n💡 Проверь cookies.txt (формат Netscape) и/или добавь PO_TOKEN_WEB."
        if error_msg:
            base += f"\n\n🔍 Детали ошибки: {error_msg}"
        else:
            base += hint
        await processing_msg.edit_text(base)


# =========================
# MAIN
# =========================
async def main():
    logger.info("Бот запущен")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
