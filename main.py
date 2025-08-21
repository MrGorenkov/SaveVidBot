import logging
import re
import os
import sys
import asyncio
import json
from datetime import datetime
from typing import Optional, Tuple, List

import yt_dlp
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

# =========================
# НАСТРОЙКИ / ENV
# =========================

# 1) Telegram токен: ENV TELEGRAM_BOT_TOKEN или сюда в fallback
TOKEN_FALLBACK = ""  # можно вписать токен сюда, если не используешь ENV

# 2) Куки (Netscape). Ищем здесь по порядку.
DEFAULT_COOKIES_CANDIDATES = [
    "/mnt/data/cookies.txt",      # << приоритетно (загруженный файл)
    # "/etc/secrets/cookies.txt", # НЕ используем, чтобы не ловить read-only
]

# 3) PO Token:
#    - Вариант А (рекомендуется): ENV PO_TOKEN_RAW = "<сам токен без префикса>"
#      + ENV PO_TOKEN_CONTEXT = "web" ИЛИ "web.remix"
#    - Вариант Б: ENV PO_TOKEN_FULL = "web+<токен>" или "web.remix+<токен>"
#    - Совместимость: если задан YTDLP_PO_TOKENS — тоже примем.
PO_TOKEN_RAW_ENV = "PO_TOKEN_RAW"
PO_TOKEN_CONTEXT_ENV = "PO_TOKEN_CONTEXT"  # web | web.remix
PO_TOKEN_FULL_ENV = "PO_TOKEN_FULL"        # уже вида CLIENT.CONTEXT+TOKEN
PO_TOKEN_COMPAT_ENV = "YTDLP_PO_TOKENS"    # на всякий случай (если уже есть)

# 4) User-Agent (лучше тот же, что в браузере с poToken/cookies)
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
    env_path = os.getenv("COOKIES_PATH", "").strip()
    candidates: List[str] = []
    if env_path:
        candidates.append(env_path)
    candidates.extend(DEFAULT_COOKIES_CANDIDATES)

    for p in candidates:
        if not p:
            continue
        if os.path.isfile(p):
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


def build_po_token_entry() -> Optional[str]:
    """
    Возвращает строку формата 'CLIENT.CONTEXT+TOKEN' (например 'web+AAA' или 'web.remix+AAA')
    или None, если токенов нет.
    """
    full = os.getenv(PO_TOKEN_FULL_ENV, "").strip()
    if not full:
        full = os.getenv(PO_TOKEN_COMPAT_ENV, "").strip()  # совместимость, если вдруг задано
    if full:
        if "+" not in full:
            logger.warning(f"{PO_TOKEN_FULL_ENV} задан, но без '+': ожидается 'CLIENT.CONTEXT+TOKEN'")
            return None
        logger.info(f"Using PO token (full, context already provided): {full.split('+')[0]}+***")
        return full

    raw = os.getenv(PO_TOKEN_RAW_ENV, "").strip()
    if not raw:
        return None

    context = os.getenv(PO_TOKEN_CONTEXT_ENV, "web").strip()  # web | web.remix
    if context not in ("web", "web.remix"):
        logger.warning(f"Неизвестный PO_TOKEN_CONTEXT='{context}', используем 'web'")
        context = "web"

    entry = f"{context}+{raw}"
    logger.info(f"Using PO token (built): {context}+***")
    return entry


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
    po_entry = build_po_token_entry()  # 'web+AAA...' или 'web.remix+AAA...' или None
    YTDLP_UA = os.getenv("YTDLP_UA", DEFAULT_UA).strip()
    cookiefile = find_cookiefile()

    # базовые опции
    ydl_opts = {
        "format": "best[height<=720][filesize<50M]/best[filesize<50M]/best",
        "outtmpl": "downloaded_video.%(ext)s",
        "merge_output_format": "mp4",
        "quiet": False,
        "no_warnings": False,
        "verbose": True,
        "noplaylist": True,
        "max_filesize": 50 * 1024 * 1024,
        "http_chunk_size": 10 * 1024 * 1024,  # <= 10MB
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
            # Используем web-клиент (чтобы poToken применился)
            "youtube": {
                "player_client": ["web"],  # без 'tv' — чтобы не уходило в TV
            },
        },
    }

    # куки только для чтения (никаких /etc/secrets)
    if cookiefile:
        ydl_opts["cookiefile"] = cookiefile

    # poToken: ДОЛЖЕН быть СПИСКОМ строк 'CLIENT.CONTEXT+TOKEN'
    if po_entry:
        ydl_opts["extractor_args"]["youtube"]["po_token"] = [po_entry]
        # для вкладок/плейлистов — иногда требуется
        ydl_opts["extractor_args"]["youtubetab"] = {"po_token": [po_entry]}

    try:
        loop = asyncio.get_running_loop()

        def sync_download():
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                logger.debug(f"Starting download for URL: {url}")

                # 1) Получим info без скачивания (оценка размера)
                info = ydl.extract_info(url, download=False)
                logger.debug(f"Video info extracted: {info.get('title', 'Unknown')}")

                filesize = info.get("filesize") or info.get("filesize_approx")
                if filesize and filesize > 50 * 1024 * 1024:
                    raise Exception("Видео превышает максимальный размер (50 МБ)")

                # 2) Скачиваем
                info = ydl.extract_info(url, download=True)
                filename = ydl.prepare_filename(info)

                # если название с другим расширением — попробуем угадать
                if not os.path.exists(filename):
                    base = os.path.splitext(filename)[0]
                    for ext in [".mp4", ".webm", ".mkv", ".avi"]:
                        test = base + ext
                        if os.path.exists(test):
                            filename = test
                            break

                # стараемся иметь .mp4 (без записи куда-либо кроме рабочей директории)
                if not filename.endswith(".mp4") and os.path.exists(filename):
                    base, _ = os.path.splitext(filename)
                    new_filename = f"{base}.mp4"
                    try:
                        os.rename(filename, new_filename)
                        filename = new_filename
                    except Exception:
                        pass

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
            return None, "Требуется вход (anti-bot). Проверь cookies.txt (Netscape) и poToken."
        if "Invalid po_token configuration format" in msg:
            return None, "Неверный формат poToken. Нужен вид 'CLIENT.CONTEXT+TOKEN' (например, 'web+AAA' или 'web.remix+AAA')."
        if "The provided YouTube account cookies are no longer valid" in msg:
            return None, "Куки протухли. Экспортируй свежие cookies и замени файл."
        return None, f"Ошибка извлечения видео: {msg}"
    except Exception as e:
        logger.error(f"Error downloading video: {e}")
        err = str(e)
        if "Read-only file system" in err and "/etc/secrets/cookies.txt" in err:
            return None, "Нельзя писать в /etc/secrets. Положи cookies в /mnt/data/cookies.txt и укажи COOKIES_PATH."
        if "Video unavailable" in err:
            return None, "Видео недоступно или удалено."
        if "Private video" in err:
            return None, "Видео приватное."
        if "age" in err.lower():
            return None, "Видео с возрастными ограничениями."
        return None, f"Ошибка скачивания: {err}"


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
        "🎥 Привет! Я бот для скачивания видео.\n\n"
        "📱 Поддержка: YouTube, TikTok, Instagram, Twitter/X, Facebook и др.\n"
        "⚠️ Лимит размера: 50 МБ.\n\n"
        "Просто пришли ссылку.",
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
    favorite_platform = "Нет данных"
    favorite_count = 0
    if stats["platforms"]:
        favorite_platform = max(stats["platforms"], key=stats["platforms"].get)
        favorite_count = stats["platforms"][favorite_platform]

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
    except Exception:
        await callback.message.answer(stats_text, parse_mode="Markdown")


@dp.message()
async def handle_message(message: types.Message):
    user_id = message.from_user.id
    url = (message.text or "").strip()

    if not is_valid_url(url):
        await message.reply(
            "❌ Пришли корректную ссылку на видео.\n\n"
            "Примеры:\n"
            "• https://www.youtube.com/watch?v=...\n"
            "• https://www.tiktok.com/@user/video/...\n"
            "• https://www.instagram.com/p/...\n"
            "• https://twitter.com/user/status/..."
        )
        return

    processing_msg = await message.reply("⏳ Скачиваю...")

    result, error_msg = await download_video(url)
    if result:
        filename, title = result
        try:
            if not os.path.exists(filename):
                await processing_msg.edit_text("❌ Ошибка: файл не найден после скачивания.")
                return

            file_size = os.path.getsize(filename)
            if file_size > 50 * 1024 * 1024:
                await processing_msg.edit_text("❌ Видео слишком большое для Telegram (макс. 50 МБ).")
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

            update_user_stats(user_id, detect_platform(url), file_size)
            os.remove(filename)
            await processing_msg.delete()

        except Exception as e:
            await processing_msg.edit_text(f"❌ Не удалось отправить видео: {str(e)}.")
            if os.path.exists(filename):
                try:
                    os.remove(filename)
                except Exception:
                    pass
    else:
        base = "❌ Не удалось скачать видео."
        hint = "\n\n💡 Проверь cookies.txt (Netscape) и poToken."
        if error_msg:
            base += f"\n\n🔍 Детали: {error_msg}"
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
