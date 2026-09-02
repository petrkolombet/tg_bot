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

# --- Основной провайдер ( OpenRouter / g4f.space ) ---
G4F_URL = os.getenv('G4F_URL', 'https://g4f.space/api/gemini')
G4F_KEY = os.getenv('G4F_KEY', '')
G4F_MODEL = os.getenv('G4F_MODEL', 'models/gemini-3.5-flash')

# --- Ключи отдельных провайдеров ---
OPENROUTER_KEY = os.getenv('OPENROUTER_KEY', '')
GEMINI_KEY = os.getenv('GEMINI_KEY', 'sk-gemini')
ALICE_KEY = os.getenv('ALICE_KEY', 'sk-alice')
DEEPSEEK_KEY = os.getenv('DEEPSEEK_KEY', 'sk-freedeepseek')
GPT_KEY = os.getenv('GPT_KEY', 'anon')

# --- RAG (память; сейчас через DeepSeek) ---
RAG_URL = os.getenv('RAG_URL', 'http://127.0.0.1:9655')
RAG_KEY = os.getenv('RAG_KEY', 'sk-freedeepseek')
RAG_MODEL = os.getenv('RAG_MODEL', 'deepseek-chat')
RAG_FALLBACK_URL = os.getenv('RAG_FALLBACK_URL', '')
RAG_FALLBACK_KEY = os.getenv('RAG_FALLBACK_KEY', '')
RAG_FALLBACK_MODEL = os.getenv('RAG_FALLBACK_MODEL', '')

# --- Прокси отдельных провайдеров (формат: host:port:user:pass или пусто) ---
PROXY_OPENROUTER = os.getenv('PROXY_OPENROUTER', '')
PROXY_G4F = os.getenv('PROXY_G4F', '')
PROXY_GEMINI = os.getenv('PROXY_GEMINI', '')
PROXY_ALICE = os.getenv('PROXY_ALICE', '')
PROXY_DEEPSEEK = os.getenv('PROXY_DEEPSEEK', '')
PROXY_GPT = os.getenv('PROXY_GPT', '')

# --- Фолбек основной генерации ---
MAIN_FALLBACK_URL = os.getenv('MAIN_FALLBACK_URL', '')
MAIN_FALLBACK_KEY = os.getenv('MAIN_FALLBACK_KEY', '')
MAIN_FALLBACK_MODEL = os.getenv('MAIN_FALLBACK_MODEL', '')

# --- Поиск (агентный поиск; сейчас через Alice) ---
SEARCH_PROXY_URL = os.getenv('SEARCH_PROXY_URL', 'http://127.0.0.1:8000')
SEARCH_PROXY_KEY = os.getenv('SEARCH_PROXY_KEY', 'sk-alice')
SEARCH_MODEL = os.getenv('SEARCH_MODEL', 'yandex-alice')

# --- Фолбек поиска ---
SEARCH_FALLBACK_URL = os.getenv('SEARCH_FALLBACK_URL', '')
SEARCH_FALLBACK_KEY = os.getenv('SEARCH_FALLBACK_KEY', '')
SEARCH_FALLBACK_MODEL = os.getenv('SEARCH_FALLBACK_MODEL', '')

# --- Саммари / выжимки (отдельный провайдер) ---
SUMMARY_PROXY_URL = os.getenv('SUMMARY_PROXY_URL', SEARCH_PROXY_URL)
SUMMARY_PROXY_KEY = os.getenv('SUMMARY_PROXY_KEY', SEARCH_PROXY_KEY)
SUMMARY_MODEL = os.getenv('SUMMARY_MODEL', SEARCH_MODEL)

# --- ChatGPT фолбек (старый, для обратной совместимости) ---
CHATGPT_FALLBACK_URL = os.getenv('CHATGPT_FALLBACK_URL', 'http://127.0.0.1:5040')
CHATGPT_FALLBACK_KEY = os.getenv('CHATGPT_FALLBACK_KEY', 'anon')

# --- Фолбек саммари ---
SUMMARY_FALLBACK_URL = os.getenv('SUMMARY_FALLBACK_URL', '')
SUMMARY_FALLBACK_KEY = os.getenv('SUMMARY_FALLBACK_KEY', '')
SUMMARY_FALLBACK_MODEL = os.getenv('SUMMARY_FALLBACK_MODEL', '')

# --- Рефлексия / фоновые мысли ---
REFLECTION_PROXY_URL = os.getenv('REFLECTION_PROXY_URL', SUMMARY_PROXY_URL)
REFLECTION_PROXY_KEY = os.getenv('REFLECTION_PROXY_KEY', SUMMARY_PROXY_KEY)
REFLECTION_MODEL = os.getenv('REFLECTION_MODEL', SUMMARY_MODEL)
REFLECTION_TEMPERATURE = float(os.getenv('REFLECTION_TEMPERATURE', '0.7'))

# --- Фолбек рефлексии ---
REFLECTION_FALLBACK_URL = os.getenv('REFLECTION_FALLBACK_URL', '')
REFLECTION_FALLBACK_KEY = os.getenv('REFLECTION_FALLBACK_KEY', '')
REFLECTION_FALLBACK_MODEL = os.getenv('REFLECTION_FALLBACK_MODEL', '')

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

def chat_completions_url(base_url):
    """Нормализует базовый URL OpenWebUI/OpenAI-совместимого сервера и возвращает
    полный путь /v1/chat/completions. Гасит двойной /v1 (когда базовый URL уже
    содержит /v1, например http://127.0.0.1:4984/v1)."""
    if not base_url:
        return base_url
    url = base_url.rstrip("/")
    if url.endswith("/v1"):
        url = url[:-3]
    return url + "/v1/chat/completions"

def get_provider_name(url):
    """Определяет имя провайдера по URL для логов."""
    if not url:
        return "UNKNOWN"
    url_lower = url.lower()
    if "openrouter" in url_lower:
        return "OPENROUTER"
    if "g4f.space" in url_lower:
        return "G4F"
    if "127.0.0.1:4984" in url or "localhost:4984" in url:
        return "GEMINI"
    if "127.0.0.1:9655" in url or "localhost:9655" in url:
        return "DEEPSEEK"
    if "127.0.0.1:5040" in url or "localhost:5040" in url:
        return "GPT"
    if "127.0.0.1:8000" in url or "localhost:8000" in url:
        return "ALICE"
    return url.split("//")[-1].split("/")[0].split(":")[0].upper()[:12]

def parse_proxy(proxy_str):
    """Парсит прокси из формата host:port:user:pass → URL http://user:pass@host:port.
    Возвращает URL строку или None если пусто/невалидно."""
    if not proxy_str or not proxy_str.strip():
        return None
    parts = proxy_str.strip().split(":")
    if len(parts) == 4:
        host, port, user, password = parts
        return f"http://{user}:{password}@{host}:{port}"
    elif len(parts) == 2:
        host, port = parts
        return f"http://{host}:{port}"
    return None

def proxy_to_env(proxy_url):
    """Конвертирует URL прокси обратно в формат host:port:user:pass для .env."""
    if not proxy_url:
        return ""
    url = proxy_url.replace("http://", "").replace("https://", "")
    if "@" in url:
        creds, hostport = url.split("@", 1)
        user, password = creds.split(":", 1)
        return f"{hostport}:{user}:{password}"
    return url

# Маппинг provider_name → env_key для прокси
PROXY_ENV_KEYS = {
    "openrouter": "PROXY_OPENROUTER",
    "g4f": "PROXY_G4F",
    "gemini": "PROXY_GEMINI",
    "alice": "PROXY_ALICE",
    "deepseek": "PROXY_DEEPSEEK",
    "gpt": "PROXY_GPT",
}