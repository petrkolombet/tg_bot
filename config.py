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
API_KEYS = []


# --- Прокси ---
PROXY_URL = os.getenv('PROXY_URL')

# --- Gemini Proxy (OpenAI-совместимый) ---
GEMINI_PROXY_URL = os.getenv('GEMINI_PROXY_URL', 'http://127.0.0.1:4984')
GEMINI_PROXY_KEY = os.getenv('GEMINI_PROXY_KEY', 'sk-gemini')
GEMINI_MODEL = os.getenv('GEMINI_MODEL', 'gemini-3.6-flash')

# --- DeepSeek (для поиска) ---
DEEPSEEK_PROXY_URL = os.getenv('DEEPSEEK_PROXY_URL', 'http://127.0.0.1:9655')
DEEPSEEK_PROXY_KEY = os.getenv('DEEPSEEK_PROXY_KEY', 'sk-freedeepseek')
DEEPSEEK_MODEL = os.getenv('DEEPSEEK_MODEL', 'deepseek-chat')

# --- Groq (для транскрипции голосовых) ---
GROQ_KEYS = []
try:
    with open('/root/ai-chat/.groq_keys') as f:
        GROQ_KEYS = [line.strip() for line in f if line.strip()]
except FileNotFoundError:
    pass

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