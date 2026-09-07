import os
from pathlib import Path
from dotenv import load_dotenv

# Загружаем переменные окружения из .env
load_dotenv()

# ================= НАСТРОЙКИ БОТА =================

BASE_DIR = Path(__file__).resolve().parent


# --- Основные ---
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
ALLOWED_USER_ID = int(os.getenv('ALLOWED_USER_ID', 0))
BOT_VERSION = "30.0 (Stable Architecture)"

# --- Ключи API (Gemini) ---
API_KEYS = []

# --- Прокси (глобальный для Playwright и пр., НЕ для провайдеров) ---
PROXY_URL = os.getenv('PROXY_URL')

# --- Прокси для Telegram (python-telegram-bot/httpx). Если задан —
# Telegram идёт через прокси; если пуст — напрямую. Остальное не трогает. ---
TELEGRAM_PROXY = os.getenv('TELEGRAM_PROXY')

# --- Groq (для транскрипции голосовых) ---
GROQ_KEYS = []
_env_groq = os.getenv('GROQ_KEYS', '')
if _env_groq:
    GROQ_KEYS = [k.strip() for k in _env_groq.split(',') if k.strip()]
if not GROQ_KEYS:
    try:
        with open('/root/ai-chat/.groq_keys') as f:
            GROQ_KEYS = [line.strip() for line in f if line.strip()]
    except FileNotFoundError:
        pass

# --- Имена файлов ---
STATE_FILE = "state.json"
PROMPT_FILE = "prompt_template.txt"
INCOMING_DIR = "workspace/incoming"  # каталог для входящих файлов от пользователя
LLM_DEBUG_FILE = "last_gemini_prompt.txt"  # последний промпт, ушедший в Gemini (перезаписывается)

# --- Временные интервалы ---
CHECK_INTERVAL_SECONDS = 60
REFLECTION_INTERVAL_HOURS = 1
SILENCE_BEFORE_REFLECTION_HOURS = 0.15
SILENCE_BEFORE_PROACTIVE_MINUTES = 30
MAX_BACKGROUND_THOUGHTS = 3  # ротация фоновых мыслей: оставляем только последние N (в промт идёт N мыслей)

# --- Будильники и темы ---
ALARM_MAX_MISSES = 2
ALARM_CONTEXT_SNIPPET = 8
INTEREST_FOLLOWUP_COOLDOWN_MINUTES = 30

# --- Вывод инструментов в истории ---
TOOL_RESULT_LIMIT = 500  # вывод ≤500 → целиком в историю; >500 → выжимка/факт
SUMMARY_INPUT_LIMIT = 1500  # в выжимку слать максимум столько симв. вывода (хвост отбрасывается)
MAX_TOOLS_TURN = 8  # максимум инструментов (команды/поиск/воспоминания) подряд на одно сообщение; дальше — принудительный текстовый ответ
CHAT_HISTORY_LIMIT = 50  # лимит истории сообщений (для модели и триггера саммари)

# --- Параметры "человечности" ---
TYPO_CHANCE = 0.15
FALLBACK_PHRASES = ["отвлекли щас", "телега подвисла кажется", "подожди", "щас", "сек"]

# --- Параметры рефлексии ---
REFLECTION_TEMPERATURE = float(os.getenv('REFLECTION_TEMPERATURE', '0.7'))

# --- Проверка критических переменных ---
if not TELEGRAM_TOKEN:
    raise ValueError("❌ TELEGRAM_TOKEN не найден в .env файле!")
if not ALLOWED_USER_ID:
    raise ValueError("❌ ALLOWED_USER_ID не найден в .env файле!")
