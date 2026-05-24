# SaveVidBot

![Python](https://img.shields.io/badge/Python-3.10+-3776AB?style=flat-square&logo=python&logoColor=white)
![aiogram](https://img.shields.io/badge/aiogram-3.x-2CA5E0?style=flat-square&logo=telegram&logoColor=white)
![yt-dlp](https://img.shields.io/badge/yt--dlp-FF0000?style=flat-square&logo=youtube&logoColor=white)

Telegram-бот для скачивания видео из YouTube, TikTok, Instagram, Twitter/X, VK и других площадок, поддерживаемых yt-dlp.

## Возможности

- Распознавание ссылки на любой поддерживаемый ресурс из текстового сообщения
- Асинхронная загрузка видео через yt-dlp, отправка пользователю в виде файла
- Поддержка cookies-файла для приватных и возрастных видео
- Личная статистика пользователя: число загрузок по платформам и суммарный объём
- Подсказки и сообщения об ошибках на русском языке

## Стек

- aiogram 3 — асинхронный фреймворк для Telegram Bot API
- yt-dlp — обёртка для загрузки видео с >1000 поддерживаемых сайтов
- asyncio для обработки одновременных запросов
- JSON-хранилище статистики пользователей

## Запуск

```bash
pip install -r requirements.txt
export TELEGRAM_BOT_TOKEN=your_token_here
python main.py
```

Опционально: положить файл `cookies.txt` (формат Netscape) рядом с `main.py` для скачивания приватных видео.

## Структура

```
SaveVidBot/
├── main.py            # обработчики и логика загрузки
├── requirements.txt   # зависимости
└── README.md
```
