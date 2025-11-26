import os
from dotenv import load_dotenv

# Загружаем переменные окружения из .env
load_dotenv()

# ================= НАСТРОЙКИ БОТА =================

# --- Основные ---
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
ALLOWED_USER_ID = int(os.getenv('ALLOWED_USER_ID', 0))
BOT_VERSION = "30.0 (Stable Architecture)"

# --- Ключи API (Gemini) ---
API_KEYS = [
    os.getenv('API_KEY_1'),
    os.getenv('API_KEY_2'),
    os.getenv('API_KEY_3')
]
# Фильтруем None значения на случай, если какой-то ключ не задан
API_KEYS = [key for key in API_KEYS if key]

# --- Прокси ---
PROXY_URL = os.getenv('PROXY_URL')

# --- Имена файлов ---
STATE_FILE = "state.json"
PROMPT_FILE = "prompt_template.txt"

# --- Временные интервалы ---
CHECK_INTERVAL_SECONDS = 60
REFLECTION_INTERVAL_HOURS = 1
SILENCE_BEFORE_REFLECTION_HOURS = 0.15
SILENCE_BEFORE_PROACTIVE_MINUTES = 30

# --- Параметры "человечности" ---
TYPO_CHANCE = 0.15
FALLBACK_PHRASES = ["отвлекли щас", "телега подвисла кажется", "подожди", "щас", "сек"]

# --- Проверка критических переменных ---
if not TELEGRAM_TOKEN:
    raise ValueError("❌ TELEGRAM_TOKEN не найден в .env файле!")
if not ALLOWED_USER_ID:
    raise ValueError("❌ ALLOWED_USER_ID не найден в .env файле!")
if not API_KEYS:
    raise ValueError("❌ API ключи не найдены в .env файле!")