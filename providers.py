"""ЕДИНСТВЕННЫЙ конфиг и точка доступа к LLM-провайдерам бота.

Здесь живёт ВСЁ, что связано с провайдерами:
  - чтение настроек из .env (url/key/model/proxy);
  - реестр провайдеров, секции и фолбеки;
  - списки моделей (в т.ч. динамическая загрузка free-моделей OpenRouter);
  - вся сетевая логика: request(), open_url() (прокси по провайдеру).

ПРАВИЛА:
  - Другие модули НЕ определяют и НЕ хранят провайдеров. Они спрашивают здесь.
  - Внешние запросы идут ТОЛЬКО через providers.request() / providers.open_url().
    Прямых urllib.request.urlopen в коде вне этого файла быть не должно
    (проверка: grep 'urllib.request.urlopen' — ноль кроме этого файла).
  - config.py не знает провайдеров: здесь читаем os.getenv напрямую.
  - /models пишет настройки в .env, здесь они читаются при каждом старте.
"""
import json
import logging
import os
import urllib.parse
import urllib.request

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

LOCAL_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0", "::1"}
DEFAULT_TIMEOUT = 180


# ================= Парсинг / нормализация =================

def chat_completions_url(base_url):
    """Нормализует базовый URL OpenAI-совместимого сервера в полный путь
    /v1/chat/completions. Гасит двойной /v1 (http://127.0.0.1:4984/v1)."""
    if not base_url:
        return base_url
    url = base_url.rstrip("/")
    if url.endswith("/v1"):
        url = url[:-3]
    return url + "/v1/chat/completions"


def embeddings_url(base_url):
    """Нормализует базовый URL в полный путь /v1/embeddings (OpenAI-совместимый)."""
    if not base_url:
        return base_url
    url = base_url.rstrip("/")
    if url.endswith("/v1"):
        url = url[:-3]
    return url + "/v1/embeddings"


def parse_proxy(proxy_str):
    """Парсит прокси host:port:user:pass → http://user:pass@host:port или None."""
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
    """Конвертирует URL прокси обратно в формат host:port:user:pass."""
    if not proxy_url:
        return ""
    url = proxy_url.replace("http://", "").replace("https://", "")
    if "@" in url:
        creds, hostport = url.split("@", 1)
        user, password = creds.split(":", 1)
        return f"{hostport}:{user}:{password}"
    return url


def get_provider_name(url):
    """Определяет имя провайдера по URL (для логов и выбора прокси)."""
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


# ================= Чтение .env (провайдеры) =================

# Та же переменная, что и в .env — единый источник. Публично для /models.

def _env(key, default=""):
    return os.getenv(key, default)


def env_named(prefix):
    """Возвращает dict префикс_* из .env: URL/KEY/MODEL/FALLBACK_URL/..."""
    return {
        "url": _env(f"{prefix}_PROXY_URL", _env(f"{prefix}_URL", "")),
        "key": _env(f"{prefix}_PROXY_KEY", _env(f"{prefix}_KEY", "")),
        "model": _env(f"{prefix}_MODEL", ""),
    }


# --- Ключи провайдеров ---
OPENROUTER_KEY = _env("OPENROUTER_KEY")
GEMINI_KEY = _env("GEMINI_KEY", "sk-gemini")
ALICE_KEY = _env("ALICE_KEY", "sk-alice")
DEEPSEEK_KEY = _env("DEEPSEEK_KEY", "sk-freedeepseek")
GPT_KEY = _env("GPT_KEY", "anon")

# --- Основная генерация (main) ---
G4F_URL = _env("G4F_URL", "https://g4f.space/api/gemini")
G4F_KEY = _env("G4F_KEY", "")
G4F_MODEL = _env("G4F_MODEL", "models/gemini-3.5-flash")

# --- Прокси провайдеров (host:port:user:pass или пусто) ---
PROXY_OPENROUTER = _env("PROXY_OPENROUTER")
PROXY_G4F = _env("PROXY_G4F")
PROXY_GEMINI = _env("PROXY_GEMINI")
PROXY_ALICE = _env("PROXY_ALICE")
PROXY_DEEPSEEK = _env("PROXY_DEEPSEEK")
PROXY_GPT = _env("PROXY_GPT")

# --- Фолбек основной генерации ---
MAIN_FALLBACK_URL = _env("MAIN_FALLBACK_URL")
MAIN_FALLBACK_KEY = _env("MAIN_FALLBACK_KEY")
MAIN_FALLBACK_MODEL = _env("MAIN_FALLBACK_MODEL")

# --- Поиск ---
SEARCH_PROXY_URL = _env("SEARCH_PROXY_URL", "http://127.0.0.1:8000")
SEARCH_PROXY_KEY = _env("SEARCH_PROXY_KEY", "sk-alice")
SEARCH_MODEL = _env("SEARCH_MODEL", "yandex-alice")
SEARCH_FALLBACK_URL = _env("SEARCH_FALLBACK_URL")
SEARCH_FALLBACK_KEY = _env("SEARCH_FALLBACK_KEY")
SEARCH_FALLBACK_MODEL = _env("SEARCH_FALLBACK_MODEL")

# --- Саммари / выжимки ---
SUMMARY_PROXY_URL = _env("SUMMARY_PROXY_URL", SEARCH_PROXY_URL)
SUMMARY_PROXY_KEY = _env("SUMMARY_PROXY_KEY", SEARCH_PROXY_KEY)
SUMMARY_MODEL = _env("SUMMARY_MODEL", SEARCH_MODEL)
SUMMARY_FALLBACK_URL = _env("SUMMARY_FALLBACK_URL")
SUMMARY_FALLBACK_KEY = _env("SUMMARY_FALLBACK_KEY")
SUMMARY_FALLBACK_MODEL = _env("SUMMARY_FALLBACK_MODEL")

# --- ChatGPT фолбек (старый, обратная совместимость) ---
CHATGPT_FALLBACK_URL = _env("CHATGPT_FALLBACK_URL", "http://127.0.0.1:5040")
CHATGPT_FALLBACK_KEY = _env("CHATGPT_FALLBACK_KEY", "anon")

# --- Рефлексия / фоновые мысли ---
REFLECTION_PROXY_URL = _env("REFLECTION_PROXY_URL", SUMMARY_PROXY_URL)
REFLECTION_PROXY_KEY = _env("REFLECTION_PROXY_KEY", SUMMARY_PROXY_KEY)
REFLECTION_MODEL = _env("REFLECTION_MODEL", SUMMARY_MODEL)
REFLECTION_FALLBACK_URL = _env("REFLECTION_FALLBACK_URL")
REFLECTION_FALLBACK_KEY = _env("REFLECTION_FALLBACK_KEY")
REFLECTION_FALLBACK_MODEL = _env("REFLECTION_FALLBACK_MODEL")

# --- RAG (память) ---
RAG_URL = _env("RAG_URL", "http://127.0.0.1:9655/v1")
RAG_KEY = _env("RAG_KEY", "sk-freedeepseek")
RAG_MODEL = _env("RAG_MODEL", "deepseek-chat")
RAG_FALLBACK_URL = _env("RAG_FALLBACK_URL")
RAG_FALLBACK_KEY = _env("RAG_FALLBACK_KEY")
RAG_FALLBACK_MODEL = _env("RAG_FALLBACK_MODEL")

# --- Эмбеддинги (векторная память RAG) ---
EMBED_URL = _env("EMBED_URL", "https://openrouter.ai/api/v1")
EMBED_KEY = _env("EMBED_KEY", OPENROUTER_KEY)
EMBED_MODEL = _env("EMBED_MODEL", "liquid/lfm-2.5-embedding-350m:free")
EMBED_FALLBACK_URL = _env("EMBED_FALLBACK_URL")
EMBED_FALLBACK_KEY = _env("EMBED_FALLBACK_KEY")
EMBED_FALLBACK_MODEL = _env("EMBED_FALLBACK_MODEL")

# --- Perplexity (мёртвый фолбек-поиск) ---
PERPLEXITY_COOKIE = _env("PERPLEXITY_COOKIE")
PERPLEXITY_RW_TOKEN = _env("PERPLEXITY_RW_TOKEN")


# ================= Реестр провайдеров =================

# Маппинг provider_name → env_key для прокси
PROXY_ENV_KEYS = {
    "openrouter": "PROXY_OPENROUTER",
    "g4f": "PROXY_G4F",
    "gemini": "PROXY_GEMINI",
    "alice": "PROXY_ALICE",
    "deepseek": "PROXY_DEEPSEEK",
    "gpt": "PROXY_GPT",
}

# Готовые провайдеры: имя -> (url, key, model, proxy)
PROVIDERS = {
    "alice": ("http://127.0.0.1:8000/v1", ALICE_KEY, "yandex-alice", PROXY_ALICE),
    "gpt": ("http://127.0.0.1:5040/v1", GPT_KEY, "chatgpt", PROXY_GPT),
    "deepseek": ("http://127.0.0.1:9655/v1", DEEPSEEK_KEY, "deepseek-chat", PROXY_DEEPSEEK),
    "gemini": ("http://127.0.0.1:4984/v1", GEMINI_KEY, "gemini-3.6-flash", PROXY_GEMINI),
    "g4f": ("https://g4f.space/api/gemini", "", "models/gemini-3.5-flash", PROXY_G4F),
    "openrouter": ("https://openrouter.ai/api/v1", OPENROUTER_KEY, "minimax/minimax-m3:free", PROXY_OPENROUTER),
}

# Модели по провайдерам
PROVIDER_MODELS = {
    "g4f": [
        ("models/gemini-3-flash-preview", "Gemini 3 Flash Preview"),
        ("models/gemini-3.1-flash-lite", "Gemini 3.1 Flash Lite"),
        ("models/gemini-3.1-flash-lite-preview", "Gemini 3.1 Flash Lite Preview"),
        ("models/gemini-flash-latest", "Gemini Flash Latest"),
        ("models/gemini-3.5-flash", "Gemini 3.5 Flash"),
        ("models/gemini-3-pro-preview", "Gemini 3 Pro Preview"),
        ("models/gemini-3.1-pro-preview", "Gemini 3.1 Pro Preview"),
        ("models/gemini-2.5-flash", "Gemini 2.5 Flash"),
        ("models/gemini-2.5-pro", "Gemini 2.5 Pro"),
        ("models/gemini-2.0-flash", "Gemini 2.0 Flash"),
        ("models/gemini-2.0-flash-001", "Gemini 2.0 Flash 001"),
        ("models/gemini-2.0-flash-lite", "Gemini 2.0 Flash Lite"),
        ("models/gemini-2.0-flash-lite-001", "Gemini 2.0 Flash Lite 001"),
        ("models/gemini-2.5-flash-preview-tts", "Gemini 2.5 Flash Preview TTS"),
        ("models/gemini-2.5-pro-preview-tts", "Gemini 2.5 Pro Preview TTS"),
        ("models/gemma-4-26b-a4b-it", "Gemma 4 26B"),
        ("models/gemma-4-31b-it", "Gemma 4 31B"),
        ("models/gemini-flash-lite-latest", "Gemini Flash Lite Latest"),
        ("models/gemini-pro-latest", "Gemini Pro Latest"),
        ("models/gemini-2.5-flash-lite", "Gemini 2.5 Flash Lite"),
        ("models/gemini-2.5-flash-image", "Gemini 2.5 Flash Image"),
        ("models/gemini-3.1-pro-preview-customtools", "Gemini 3.1 Pro Custom Tools"),
        ("models/gemini-3.1-flash-lite-image", "Gemini 3.1 Flash Lite Image"),
        ("models/gemini-3-pro-image-preview", "Gemini 3 Pro Image Preview"),
        ("models/gemini-3-pro-image", "Gemini 3 Pro Image"),
        ("models/gemini-3.1-flash-image-preview", "Gemini 3.1 Flash Image Preview"),
        ("models/gemini-3.1-flash-image", "Gemini 3.1 Flash Image"),
        ("models/gemini-3.1-flash-tts-preview", "Gemini 3.1 Flash TTS"),
        ("models/gemini-omni-flash-preview", "Gemini Omni Flash"),
        ("models/gemini-3.5-live-translate-preview", "Gemini 3.5 Live Translate"),
        ("models/gemini-3.1-flash-live-preview", "Gemini 3.1 Flash Live"),
    ],
    "openrouter": [
        ("minimax/minimax-m3:free", "MiniMax M3 (free)"),
    ],
    "deepseek": [
        ("deepseek-chat", "DeepSeek Chat"),
        ("deepseek-reasoner", "DeepSeek Reasoner"),
    ],
    "gemini": [
        ("gemini-3.6-flash", "Gemini 3.6 Flash"),
        ("gemini-3.5-flash", "Gemini 3.5 Flash"),
        ("gemini-3.5-flash-thinking", "Gemini 3.5 Flash Thinking"),
        ("gemini-3.5-flash-thinking-lite", "Gemini 3.5 Flash Thinking Lite"),
        ("gemini-3.1-pro", "Gemini 3.1 Pro"),
        ("gemini-3.1-pro-enhanced", "Gemini 3.1 Pro Enhanced"),
        ("gemini-auto", "Gemini Auto"),
        ("gemini-flash-lite", "Gemini Flash Lite"),
    ],
    "alice": [
        ("yandex-alice", "Yandex Alice"),
    ],
    "gpt": [
        ("chatgpt", "ChatGPT"),
    ],
}

# Динамическая загрузка free-моделей OpenRouter
OPENROUTER_MODEL_MAP = {}

def _load_openrouter_free_models():
    """Загружает список бесплатных моделей с OpenRouter (через прокси провайдера)."""
    try:
        proxy = parse_proxy(PROXY_OPENROUTER) or parse_proxy(PROXY_G4F)
        if proxy:
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({"http": proxy, "https": proxy})
            )
        else:
            opener = urllib.request.build_opener()
        req = urllib.request.Request("https://openrouter.ai/api/v1/models")
        resp = opener.open(req, timeout=15)
        data = json.loads(resp.read().decode("utf-8"))
        free = [m for m in data.get("data", []) if ":free" in m["id"]]
        models = [(m["id"], m["id"].split("/")[-1].replace(":free", "")) for m in sorted(free, key=lambda x: x["id"])]
        if models:
            PROVIDER_MODELS["openrouter"] = models
            OPENROUTER_MODEL_MAP.clear()
            for i, (model_id, _) in enumerate(models):
                OPENROUTER_MODEL_MAP[str(i)] = model_id
            logger.info(f"✅ [OPENROUTER] Загружено {len(models)} free-моделей")
    except Exception as e:
        logger.warning(f"⚠️ [OPENROUTER] Не удалось загрузить модели: {e}")


_load_openrouter_free_models()

# Маппинг секции -> env-ключи (url, key, model)
SECTION_KEYS = {
    "main": ("G4F_URL", "G4F_KEY", "G4F_MODEL"),
    "search": ("SEARCH_PROXY_URL", "SEARCH_PROXY_KEY", "SEARCH_MODEL"),
    "summary": ("SUMMARY_PROXY_URL", "SUMMARY_PROXY_KEY", "SUMMARY_MODEL"),
    "reflection": ("REFLECTION_PROXY_URL", "REFLECTION_PROXY_KEY", "REFLECTION_MODEL"),
    "rag": ("RAG_URL", "RAG_KEY", "RAG_MODEL"),
    "embed": ("EMBED_URL", "EMBED_KEY", "EMBED_MODEL"),
}

# Фолбеки: секция -> (url_key, key_key, model_key)
FALLBACK_KEYS = {
    "main": ("MAIN_FALLBACK_URL", "MAIN_FALLBACK_KEY", "MAIN_FALLBACK_MODEL"),
    "search": ("SEARCH_FALLBACK_URL", "SEARCH_FALLBACK_KEY", "SEARCH_FALLBACK_MODEL"),
    "summary": ("SUMMARY_FALLBACK_URL", "SUMMARY_FALLBACK_KEY", "SUMMARY_FALLBACK_MODEL"),
    "reflection": ("REFLECTION_FALLBACK_URL", "REFLECTION_FALLBACK_KEY", "REFLECTION_FALLBACK_MODEL"),
    "rag": ("RAG_FALLBACK_URL", "RAG_FALLBACK_KEY", "RAG_FALLBACK_MODEL"),
    "embed": ("EMBED_FALLBACK_URL", "EMBED_FALLBACK_KEY", "EMBED_FALLBACK_MODEL"),
}


# ================= Сетевая логика =================

def proxy_for_url(url):
    """Прокси провайдера по URL, или None (локальные адреса / без прокси)."""
    if not url:
        return None
    try:
        host = (urllib.parse.urlparse(url).hostname or "").lower()
    except Exception:
        host = ""
    if host in LOCAL_HOSTS:
        return None
    name = get_provider_name(url)
    proxy_key = PROXY_ENV_KEYS.get(name.lower(), "")
    raw = globals().get(proxy_key, "") if proxy_key else ""
    return parse_proxy(raw) if raw else None


def open_url(req, timeout=DEFAULT_TIMEOUT, proxy=None):
    """ЕДИНСТВЕННАЯ точка открытия URL. Локальные адреса — напрямую,
    внешние — через прокси провайдера. proxy — явное переопределение для исключений."""
    try:
        host = (urllib.parse.urlparse(req.full_url).hostname or "").lower()
    except Exception:
        host = ""
    if host in LOCAL_HOSTS:
        return urllib.request.urlopen(req, timeout=timeout)
    if proxy is None:
        proxy = proxy_for_url(req.full_url)
    if proxy:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy})
        )
        return opener.open(req, timeout=timeout)
    return urllib.request.urlopen(req, timeout=timeout)


def request(url, key, model, messages, temperature=0.7, tag=None, timeout=DEFAULT_TIMEOUT, **payload_extra):
    """ЕДИНСТВЕННАЯ высокоуровневая функция LLM-запроса (OpenAI-совместимый).
    Прокси берётся автоматически по URL. Возвращает текст ответа или None.
    Доп. поля payload (search, user, session_type, response_format) — через **payload_extra."""
    if not url:
        return None
    tag = tag or model or "provider"
    payload_dict = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
    }
    payload_dict.update(payload_extra)

    req = urllib.request.Request(
        chat_completions_url(url),
        data=json.dumps(payload_dict).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {key}",
        },
    )

    logger.info(f"📤 [{tag}] Отправка запроса: {len(payload_dict['messages'])} сообщений")
    resp = open_url(req, timeout=timeout)
    data = json.loads(resp.read().decode("utf-8"))
    text = data["choices"][0]["message"]["content"]
    logger.info(f"📤 [{tag}] Ответ получен, длина: {len(text)} символов")
    return text


def embed(url, key, model, texts, timeout=90):
    """Эмбеддинги (OpenAI-совместимый /v1/embeddings). Прокси — автоматически по URL.
    Принимает list[str], возвращает list[list[float]] (по позиции входа) или None при ошибке."""
    if not url or not texts:
        return None
    texts = [t for t in texts if isinstance(t, str) and t.strip()]
    if not texts:
        return None
    payload = {"model": model, "input": texts}
    req = urllib.request.Request(
        embeddings_url(url),
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {key}",
        },
        method="POST",
    )
    resp = open_url(req, timeout=timeout)
    data = json.loads(resp.read().decode("utf-8"))
    out = {}
    for item in data.get("data", []):
        idx = item.get("index")
        vec = item.get("embedding")
        if idx is not None and isinstance(vec, list):
            out[int(idx)] = [float(v) for v in vec]
    return [out.get(i) for i in range(len(texts))]