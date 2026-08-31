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

# --- Gemini фолбек (g4f.space, чужие аккаунты + PoW-кредиты) ---
# Кредиты привязаны к IP прокси (бейкер tg-cake-baker печёт через него же).
G4F_URL = os.getenv('G4F_URL', 'https://g4f.space/api/gemini')
G4F_MODEL = os.getenv('G4F_MODEL', 'models/gemini-3.5-flash')
G4F_PROXY = os.getenv('G4F_PROXY', '')  # если пусто — системный https_proxy

# --- DeepSeek (ТОЛЬКО агентный поиск) ---
DEEPSEEK_PROXY_URL = os.getenv('DEEPSEEK_PROXY_URL', 'http://127.0.0.1:9655')
DEEPSEEK_PROXY_KEY = os.getenv('DEEPSEEK_PROXY_KEY', 'sk-freedeepseek')
DEEPSEEK_MODEL = os.getenv('DEEPSEEK_MODEL', 'deepseek-chat')

# --- Саммари / выжимки (отдельный провайдер) ---
# Пока SUMMARY_* не заполнены в .env — временно указывают на старый DeepSeek-прокси.
SUMMARY_PROXY_URL = os.getenv('SUMMARY_PROXY_URL', DEEPSEEK_PROXY_URL)
SUMMARY_PROXY_KEY = os.getenv('SUMMARY_PROXY_KEY', DEEPSEEK_PROXY_KEY)
SUMMARY_MODEL = os.getenv('SUMMARY_MODEL', DEEPSEEK_MODEL)

# --- ChatGPT фолбек (анонимный скрапер, когда SUMMARY-провайдер упал) ---
CHATGPT_FALLBACK_URL = os.getenv('CHATGPT_FALLBACK_URL', 'http://127.0.0.1:5040')
CHATGPT_FALLBACK_KEY = os.getenv('CHATGPT_FALLBACK_KEY', 'anon')

# --- Рефлексия / фоновые мысли (тот же SUMMARY-провайдер, своя модель) ---
REFLECTION_PROXY_URL = os.getenv('REFLECTION_PROXY_URL', SUMMARY_PROXY_URL)
REFLECTION_PROXY_KEY = os.getenv('REFLECTION_PROXY_KEY', SUMMARY_PROXY_KEY)
REFLECTION_MODEL = os.getenv('REFLECTION_MODEL', SUMMARY_MODEL)
REFLECTION_TEMPERATURE = float(os.getenv('REFLECTION_TEMPERATURE', '0.7'))

# --- Perplexity (фолбек-поиск, бесплатный web-эндпоинт) ---
PERPLEXITY_COOKIE = os.getenv('PERPLEXITY_COOKIE', '')
PERPLEXITY_RW_TOKEN = os.getenv('PERPLEXITY_RW_TOKEN', '')

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
MAX_BACKGROUND_THOUGHTS = 6  # ротация фоновых мыслей: оставляем только последние N

# --- Будильники и темы ---
ALARM_MAX_MISSES = 2
ALARM_CONTEXT_SNIPPET = 8
INTEREST_FOLLOWUP_COOLDOWN_MINUTES = 30

# --- Вывод инструментов в истории ---
TOOL_RESULT_LIMIT = 250  # вывод ≤250 → целиком в историю; >250 → выжимка/факт
SUMMARY_INPUT_LIMIT = 1500  # в DeepSeek-выжимку слать максимум столько симв. вывода (хвост отбрасывается)
MAX_TOOLS_TURN = 8  # максимум инструментов (команды/поиск/воспоминания) подряд на одно сообщение; дальше — принудительный текстовый ответ

# --- Параметры "человечности" ---
TYPO_CHANCE = 0.15
FALLBACK_PHRASES = ["отвлекли щас", "телега подвисла кажется", "подожди", "щас", "сек"]

# --- Проверка критических переменных ---
if not TELEGRAM_TOKEN:
    raise ValueError("❌ TELEGRAM_TOKEN не найден в .env файле!")
if not ALLOWED_USER_ID:
    raise ValueError("❌ ALLOWED_USER_ID не найден в .env файле!")