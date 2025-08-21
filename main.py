import logging
import re
import os
import asyncio
import yt_dlp
import json
from datetime import datetime
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from typing import Optional, Tuple

# Токен вашего бота
TOKEN = '7982997701:AAEBWEcylGxTK0PV3qZcvKqrqAoThKAGims'

# Настройка логирования с уровнем DEBUG
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.DEBUG
)
logger = logging.getLogger(__name__)

# Файл для хранения статистики пользователей
USER_STATS_FILE = 'user_stats.json'


def is_valid_url(url: str) -> bool:
    # Паттерны для различных платформ
    patterns = [
        # YouTube
        r'(?:https?://)?(?:www\.)?(?:youtube\.com/watch\?v=|youtu\.be/|youtube\.com/embed/|youtube\.com/v/|m\.youtube\.com/watch\?v=)[\w-]+',
        r'(?:https?://)?(?:www\.)?(?:tiktok\.com/@[\w.-]+/(?:video|photo)/\d+|vm\.tiktok\.com/[\w-]+|m\.tiktok\.com/v/\d+)',
        # Instagram
        r'(?:https?://)?(?:www\.)?instagram\.com/(?:p|reel|tv)/[\w-]+',
        # Twitter/X
        r'(?:https?://)?(?:www\.)?(?:twitter\.com|x\.com)/\w+/status/\d+',
        # Facebook
        r'(?:https?://)?(?:www\.)?facebook\.com/.*?/videos/\d+',
        # Общий паттерн для других платформ
        r'(?:https?://)?[\w.-]+\.[\w]{2,}(?:/[\w.-]*)*/?'
    ]

    for pattern in patterns:
        if re.match(pattern, url, re.IGNORECASE):
            return True
    return False


async def download_video(url: str) -> Tuple[Optional[Tuple[str, str, str]], Optional[str]]:
    base_ydl_opts = {
        'format': 'best[height<=720][filesize<50M]/best[filesize<50M]/best',
        'outtmpl': 'downloaded_media.%(ext)s',
        'merge_output_format': 'mp4',
        'quiet': True,
        'no_warnings': True,
        'noplaylist': True,
        'max_filesize': 50 * 1024 * 1024,
        'socket_timeout': 60,
        'retries': 3,
        'fragment_retries': 3,
        'writeinfojson': False,
        'extract_flat': False,
        'ignoreerrors': False,
    }

    def get_ydl_opts_with_cookies():
        """Try cookies from browsers following official FAQ recommendations"""
        # Firefox works best according to FAQ
        browsers_to_try = [
            ('firefox', 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:133.0) Gecko/20100101 Firefox/133.0'),
            ('safari', 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.2 Safari/605.1.15'),
            ('chrome', 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36')
        ]
        
        for browser, user_agent in browsers_to_try:
            try:
                logger.debug(f"Trying {browser} cookies with matching user-agent...")
                opts = base_ydl_opts.copy()
                opts['cookiesfrombrowser'] = (browser, None, None, None)
                opts['user_agent'] = user_agent
                
                # Test if cookies work by trying to extract info without downloading
                with yt_dlp.YoutubeDL(opts) as ydl:
                    ydl.extract_info(url, download=False)
                    logger.info(f"Successfully using {browser} cookies")
                    return opts
                    
            except Exception as e:
                logger.debug(f"Failed with {browser}: {e}")
                continue
        
        # Fallback without cookies but with a good user-agent
        logger.info("Using fallback without cookies")
        opts = base_ydl_opts.copy()
        opts['user_agent'] = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36'
        return opts

    try:
        loop = asyncio.get_running_loop()

        def sync_download():
            final_ydl_opts = get_ydl_opts_with_cookies()
            
            with yt_dlp.YoutubeDL(final_ydl_opts) as ydl:
                logger.debug(f"Starting download for URL: {url}")

                info = ydl.extract_info(url, download=False)
                logger.debug(f"Media info extracted: {info.get('title', 'Unknown')}")

                filesize = info.get('filesize') or info.get('filesize_approx')
                if filesize and filesize > 50 * 1024 * 1024:
                    raise Exception("Медиа превышает максимальный размер (50 МБ)")

                # Determine media type
                media_type = 'video'
                if '/photo/' in url or info.get('ext') in ['jpg', 'jpeg', 'png', 'webp', 'gif']:
                    media_type = 'photo'
                    final_ydl_opts['format'] = 'best'
                    final_ydl_opts['merge_output_format'] = None

                # Download the media
                with yt_dlp.YoutubeDL(final_ydl_opts) as ydl_download:
                    info = ydl_download.extract_info(url, download=True)
                    
                    if info.get('entries'):
                        info = info['entries'][0]
                    
                    filename = ydl_download.prepare_filename(info)

                    # Find the actual downloaded file
                    if not os.path.exists(filename):
                        base_name = os.path.splitext(filename)[0]
                        extensions = ['.mp4', '.webm', '.mkv', '.avi', '.jpg', '.jpeg', '.png', '.webp', '.gif']
                        for ext in extensions:
                            test_filename = base_name + ext
                            if os.path.exists(test_filename):
                                filename = test_filename
                                break

                    logger.debug(f"Downloaded {media_type}: {filename}")
                    return filename, info.get('title', 'media'), media_type

        result = await loop.run_in_executor(None, sync_download)
        return result, None

    except yt_dlp.utils.MaxDownloadsReached as e:
        logger.error("File exceeds max size")
        return None, "Медиа превышает максимальный размер (50 МБ)."
    except yt_dlp.utils.UnsupportedError as e:
        logger.error(f"Unsupported URL or format: {e}")
        return None, "Неподдерживаемая платформа или формат контента."
    except yt_dlp.utils.ExtractorError as e:
        logger.error(f"Extractor error: {e}")
        if "Requested format is not available" in str(e):
            return None, "Запрашиваемый формат недоступен. Попробуй другое видео или фото."
        return None, f"Ошибка извлечения контента: {str(e)}"
    except Exception as e:
        logger.error(f"Error downloading media: {str(e)}")
        error_msg = str(e)
        
        if "Unable to extract webpage video data" in error_msg and "tiktok" in url.lower():
            return None, (
                "TikTok изменил защиту от ботов. Для решения:\n\n"
                "1️⃣ Обнови yt-dlp до dev версии:\n"
                "pip install --force-reinstall git+https://github.com/yt-dlp/yt-dlp.git\n\n"
                "2️⃣ Или попробуй другую ссылку с TikTok\n\n"
                "3️⃣ Если не помогает - TikTok временно заблокировал доступ"
            )
        elif "Sign in to confirm you're not a bot" in error_msg or "Sign in to confirm your age" in error_msg:
            return None, "YouTube требует авторизацию. Попробуй другое видео или фото или повтори позже."
        elif "Video unavailable" in error_msg:
            return None, "Видео недоступно или удалено."
        elif "Private video" in error_msg:
            return None, "Видео является приватным."
        elif "This video is only available for registered users" in error_msg:
            return None, "Видео доступно только зарегистрированным пользователям."
        elif "Unable to extract" in error_msg:
            return None, "Не удалось извлечь контент. Возможно, платформа заблокировала доступ."
        return None, f"Ошибка скачивания: {error_msg}"


def load_user_stats():
    """Загружает статистику пользователей из файла"""
    try:
        if os.path.exists(USER_STATS_FILE):
            with open(USER_STATS_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
    except Exception as e:
        logger.error(f"Error loading user stats: {e}")
    return {}


def save_user_stats(stats):
    """Сохраняет статистику пользователей в файл"""
    try:
        with open(USER_STATS_FILE, 'w', encoding='utf-8') as f:
            json.dump(stats, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Error saving user stats: {e}")


def update_user_stats(user_id: int, platform: str, file_size: int = 0):
    """Обновляет статистику пользователя"""
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
    """Получает статистику пользователя"""
    stats = load_user_stats()
    return stats.get(str(user_id), None)


def detect_platform(url: str) -> str:
    """Определяет платформу по URL"""
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
    """Форматирует размер файла в читаемый вид"""
    if size_bytes == 0:
        return "0 Б"
    
    size_names = ["Б", "КБ", "МБ", "ГБ", "ТБ"]
    i = 0
    size = float(size_bytes)
    
    while size >= 1024.0 and i < len(size_names) - 1:
        size /= 1024.0
        i += 1
    
    return f"{size:.1f} {size_names[i]}"


# Инициализация бота и диспетчера
bot = Bot(token=TOKEN)
dp = Dispatcher()


@dp.message(CommandStart())
async def start(message: types.Message):
    user_id = message.from_user.id

    # Создаем клавиатуру с кнопкой статистики
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Моя статистика", callback_data="show_stats")]
    ])

    await message.reply(
        "🎥 Привет! Я раб Санька - бот для скачивания видео и фото!\n\n"
        "📱 Поддерживаемые платформы:\n"
        "• YouTube\n"
        "• TikTok\n"
        "• Instagram\n"
        "• Twitter/X\n"
        "• Facebook\n"
        "• И многие другие!\n\n"
        "📤 Просто отправь мне ссылку на видео или фото, и я его скачаю!\n"
        "⚠️ Максимальный размер файла: 50 МБ",
        reply_markup=keyboard
    )


@dp.callback_query(F.data == "show_stats")
async def show_stats_callback(callback: types.CallbackQuery):
    user_id = callback.from_user.id

    await callback.answer()

    stats = get_user_stats(user_id)

    if not stats:
        await callback.message.answer("📊 У тебя пока нет статистики. Скачай первое видео или фото!")
        return

    # Форматируем дату первого использования
    first_use = datetime.fromisoformat(stats['first_use']).strftime("%d.%m.%Y")
    last_activity = datetime.fromisoformat(stats['last_activity']).strftime("%d.%m.%Y %H:%M")

    # Находим самую популярную платформу
    if stats['platforms']:
        favorite_platform = max(stats['platforms'], key=stats['platforms'].get)
        favorite_count = stats['platforms'][favorite_platform]
    else:
        favorite_platform = "Нет данных"
        favorite_count = 0

    stats_text = (
        f"📊 **Твоя статистика:**\n\n"
        f"📥 Скачано контента: **{stats['downloads_count']}**\n"
        f"💾 Общий размер: **{format_file_size(stats['total_size'])}**\n"
        f"🏆 Любимая платформа: **{favorite_platform}** ({favorite_count} контента)\n"
        f"📅 Первое использование: **{first_use}**\n"
        f"🕐 Последняя активность: **{last_activity}**\n\n"
        f"🎯 **По платформам:**\n"
    )

    for platform, count in stats['platforms'].items():
        stats_text += f"• {platform}: {count} контента\n"

    try:
        await callback.message.edit_text(stats_text, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Error editing stats message: {e}")
        await callback.message.answer(stats_text, parse_mode="Markdown")


# Обработчик текстовых сообщений (ссылок)
@dp.message()
async def handle_message(message: types.Message):
    user_id = message.from_user.id

    url = message.text.strip()

    if not is_valid_url(url):
        await message.reply(
            "❌ Пожалуйста, отправь корректную ссылку на видео или фото.\n\n"
            "Примеры поддерживаемых ссылок:\n"
            "• https://www.youtube.com/watch?v=...\n"
            "• https://www.tiktok.com/@user/video/...\n"
            "• https://www.instagram.com/p/...\n"
            "• https://twitter.com/user/status/..."
        )
        return

    processing_msg = await message.reply("⏳ Обрабатываю твою ссылку, скачиваю контент...")

    result, error_msg = await download_video(url)
    if result:
        filename, title, media_type = result
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
                    "❌ Файл слишком большой для Telegram (макс. 50 МБ). Попробуй другой контент.")
                os.remove(filename)
                return

            await processing_msg.edit_text(f"📤 Отправляю {media_type}...")

            # Добавляем кнопку статистики к ответу
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📊 Моя статистика", callback_data="show_stats")]
            ])

            if media_type == 'photo':
                await message.reply_photo(
                    photo=types.FSInputFile(filename),
                    caption=f"✅ Скачано: {title}",
                    reply_markup=keyboard
                )
            else:
                await message.reply_video(
                    video=types.FSInputFile(filename),
                    caption=f"✅ Скачано: {title}",
                    supports_streaming=True,
                    reply_markup=keyboard
                )

            # Обновляем статистику пользователя после успешной загрузки
            platform = detect_platform(url)
            update_user_stats(user_id, platform, file_size)

            logger.debug(f"{media_type.capitalize()} sent successfully: {filename}")
            os.remove(filename)
            await processing_msg.delete()

        except Exception as e:
            logger.error(f"Error sending {media_type}: {str(e)}")
            await processing_msg.edit_text(f"❌ Не удалось отправить {media_type}: {str(e)}. Попробуй другую ссылку.")
            if os.path.exists(filename):
                os.remove(filename)
    else:
        if "TikTok изменил защиту" in error_msg:
            reply_text = f"❌ {error_msg}"
        else:
            reply_text = "❌ Не удалось скачать контент. Проверь ссылку или попробуй позже.\n\n💡 Возможно, поможет обновление yt-dlp:\npip install git+https://github.com/yt-dlp/yt-dlp.git"
            if error_msg:
                reply_text += f"\n\n🔍 Детали ошибки: {error_msg}"
        await processing_msg.edit_text(reply_text)


# Основная функция
async def main():
    logger.info("Бот запущен")
    await dp.start_polling(bot)


if __name__ == '__main__':
    asyncio.run(main())
