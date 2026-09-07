# --- START OF FILE bot_ai.py ---

import logging
import asyncio
import json
import re
import os
import base64
import urllib.request
import urllib.parse
from datetime import datetime, timedelta, timezone

import config
import providers
from rag import remember_window, query_rag, format_memory_answer
import tools_registry

logger = logging.getLogger(__name__)

# --- Остановка текущей задачи ---
_stop_event = asyncio.Event()

def request_stop():
    """Поставить флаг остановки текущей генерации."""
    _stop_event.set()

def is_stopped():
    """Проверить, запрошена ли остановка. Сбрасывает флаг."""
    if _stop_event.is_set():
        _stop_event.clear()
        return True
    return False

def clean_json_response(text):
    # Вытаскиваем самый первый сбалансированный JSON-объект (от { до парной }),
    # чтобы не захватывать хвост/дубль (модели иногда оборачивают JSON в ```json ... ```).
    match = re.search(r'\{', text)
    if not match:
        return text.strip()
    start = match.start()
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == '\\':
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0:
                result = text[start:i+1].strip()
                break
    else:
        return text.strip()
    # Чиним частые косяки моделей: лишняя ) или ] после строки в JSON
    result = re.sub(r'"\s*\)\s*,\s*"', '", "', result)
    result = re.sub(r'"\s*\)\s*\]', '"]', result)
    # Убираем trailing comma перед } или ]
    result = re.sub(r',\s*([}\]])', r'\1', result)
    # Чиним вложенный JSON: если replies[0] — строка с JSON, извлекаем её
    try:
        parsed = json.loads(result)
        if isinstance(parsed.get("replies"), list) and len(parsed["replies"]) == 1:
            inner = parsed["replies"][0]
            if isinstance(inner, str) and inner.strip().startswith("{"):
                # Пытаемся распарсить внутренний JSON
                inner_clean = inner.strip()
                # Чиним незакрытые скобки во внутреннем JSON
                if inner_clean.count("{") > inner_clean.count("}"):
                    inner_clean += "}" * (inner_clean.count("{") - inner_clean.count("}"))
                if inner_clean.count("[") > inner_clean.count("]"):
                    inner_clean += "]" * (inner_clean.count("[") - inner_clean.count("]"))
                try:
                    inner_parsed = json.loads(inner_clean)
                    # Заменяем строку на распарсенный объект
                    parsed["replies"] = inner_parsed.get("replies", parsed["replies"])
                    if "mood_shift" in inner_parsed:
                        parsed["mood_shift"] = inner_parsed["mood_shift"]
                    if "reaction" in inner_parsed:
                        parsed["reaction"] = inner_parsed["reaction"]
                    if "thought" in inner_parsed:
                        parsed["thought"] = inner_parsed["thought"]
                    return json.dumps(parsed, ensure_ascii=False)
                except json.JSONDecodeError:
                    pass
        return result
    except json.JSONDecodeError:
        return result

def render_role(role):
    if role in ("tool", "thought"):
        return role
    return "user" if role == "user" else "assistant"

def _g4f_urlopen(req, timeout=180, proxy=None):
    """Открывает URL. Тонкий враппер вокруг providers.open_url:
    локальные адреса идут напрямую, внешние — через прокси провайдера."""
    return providers.open_url(req, timeout=timeout, proxy=proxy)


def _proxy_for_url(base_url):
    """Прокси провайдера по URL (или None для локальных адресов). Деллегирует в providers."""
    return providers.proxy_for_url(base_url)


async def safe_generate_content_g4f(prompt, temperature=0.85, image_path=None):
    """Генерация через основной провайдер ( OpenRouter / g4f.space ). Ретраи при 429/сбоях."""
    if is_stopped():
        return None
    provider = providers.get_provider_name(providers.G4F_URL)
    # Определяем прокси по провайдеру
    proxy_key = providers.PROXY_ENV_KEYS.get(provider.lower(), "")
    proxy_str = getattr(providers, proxy_key, "") if proxy_key else ""
    proxy = providers.parse_proxy(proxy_str) if proxy_str else None
    content = prompt
    if image_path:
        data, mime = _read_image_b64(image_path)
        if data:
            b64 = base64.b64encode(data).decode("ascii")
            content = [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}}
            ]
            logger.info(f"🖼️ [{provider}] Картинка прикреплена: {image_path} ({len(data)} байт)")
    payload = json.dumps({
        "model": providers.G4F_MODEL,
        "messages": [{"role": "user", "content": content}],
        "temperature": temperature
    }).encode('utf-8')
    url = f"{providers.G4F_URL}/chat/completions"

    headers = {"Content-Type": "application/json"}
    # Ключ берём из конфига провайдера
    if providers.G4F_KEY:
        headers["Authorization"] = f"Bearer {providers.G4F_KEY}"
    elif providers.OPENROUTER_KEY:
        headers["Authorization"] = f"Bearer {providers.OPENROUTER_KEY}"

    # Признаки ошибки модели (возвращаются как текст, а не HTTP)
    G4F_ERROR_TEXTS = {
        "an error occurred. please try again.",
        "internal server error",
        "oops! something went wrong",
    }

    for attempt in range(4):
        if is_stopped():
            return None
        try:
            req = urllib.request.Request(
                url,
                data=payload,
                headers=headers,
            )
            loop = asyncio.get_running_loop()
            resp = await loop.run_in_executor(None, lambda: _g4f_urlopen(req, proxy=proxy))
            raw = resp.read().decode('utf-8')
            data = json.loads(raw)
            text = data["choices"][0]["message"]["content"]

            # Логируем сырой ответ в файл
            try:
                with open(str(config.BASE_DIR / "last_raw_response.txt"), "w") as f:
                    f.write(f"=== [{provider}] Попытка {attempt+1} ===\n")
                    f.write(f"URL: {url}\n")
                    f.write(f"Модель: {providers.G4F_MODEL}\n")
                    f.write(f"Ответ ({len(text)} симв):\n{text}\n")
                    f.write(f"--- RAW ({len(raw)} симв) ---\n{raw[:2000]}\n")
            except Exception:
                pass

            if text and text.strip().lower() in G4F_ERROR_TEXTS:
                logger.warning(f"⚠️ [{provider}] Модель вернула ошибку (попытка {attempt+1}), ретраю")
                if attempt < 3:
                    await asyncio.sleep(2)
                    continue
            return text
        except Exception as e:
            logger.error(f"❌ [{provider}] Ошибка (попытка {attempt+1}): {e}")
            if attempt < 3:
                await asyncio.sleep(3)
    return None

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}

def _read_image_b64(path):
    """Читает файл-картинку и возвращает (base64_str, mime). None если не картинка.
    Если картинка больше 1024px по любой стороне — уменьшает до 1024px (пропорции сохраняются)."""
    try:
        ext = os.path.splitext(path)[1].lower()
        if ext not in IMAGE_EXTS:
            return None, None
        with open(path, "rb") as f:
            data = f.read()
        mime = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
                "webp": "image/webp", "gif": "image/gif", "bmp": "image/bmp"}.get(ext.lstrip("."), "image/png")
        # Ресайз если картинка больше 1024px по любой стороне
        if len(data) > 100_000:
            try:
                from PIL import Image
                import io
                img = Image.open(io.BytesIO(data))
                w, h = img.size
                if max(w, h) > 1024:
                    ratio = 1024 / max(w, h)
                    new_w, new_h = int(w * ratio), int(h * ratio)
                    img = img.resize((new_w, new_h), Image.LANCZOS)
                    buf = io.BytesIO()
                    save_fmt = {"jpg": "JPEG", "jpeg": "JPEG", "png": "PNG",
                                "webp": "WEBP", "gif": "GIF", "bmp": "BMP"}.get(ext.lstrip("."), "JPEG")
                    img.save(buf, format=save_fmt, quality=85)
                    data = buf.getvalue()
                    logger.info(f"🖼️ [IMAGE] Ресайз {w}x{h} → {new_w}x{new_h} ({len(data)} байт)")
            except Exception as e:
                logger.warning(f"⚠️ [IMAGE] Не удалось уменьшить картинку: {e}")
        return data, mime
    except Exception as e:
        logger.warning(f"⚠️ [IMAGE] Не удалось прочитать картинку {path}: {e}")
        return None, None


async def _try_generate(url, key, model, prompt, temperature, image_path, provider_name, attempt_limit=2, proxy=None):
    """Пытается сгенерировать через указанный провайдер. Возвращает текст или None."""
    if not url:
        return None
    content = prompt
    if image_path:
        data, mime = _read_image_b64(image_path)
        if data:
            b64 = base64.b64encode(data).decode("ascii")
            content = [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}}
            ]
    payload = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": content}],
        "temperature": temperature
    }).encode('utf-8')
    full_url = f"{url}/chat/completions"
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"

    # Определяем прокси для запроса
    parsed_proxy = providers.parse_proxy(proxy) if proxy else None

    for attempt in range(attempt_limit):
        if is_stopped():
            return None
        try:
            req = urllib.request.Request(full_url, data=payload, headers=headers)
            loop = asyncio.get_running_loop()
            resp = await loop.run_in_executor(None, lambda: _g4f_urlopen(req, proxy=parsed_proxy))
            data = json.loads(resp.read().decode('utf-8'))
            text = data["choices"][0]["message"]["content"]
            if text and text.strip().lower() in {"an error occurred. please try again.", "internal server error", "oops! something went wrong"}:
                logger.warning(f"⚠️ [{provider_name}] Модель вернула ошибку (попытка {attempt+1})")
                if attempt < attempt_limit - 1:
                    await asyncio.sleep(2)
                    continue
            return text
        except Exception as e:
            logger.error(f"❌ [{provider_name}] Ошибка (попытка {attempt+1}): {e}")
            if attempt < attempt_limit - 1:
                await asyncio.sleep(3)
    return None


async def safe_generate_content(prompt, temperature=0.85, image_path=None, channel=None):
    """Генерация с фолбеком: основной провайдер → MAIN_FALLBACK.
    При image_path — отдельная секция IMAGE (каналы IMAGE_URL/IMAGE_FALLBACK_URL),
    т.к. картинки принимает не каждый провайдер (свой Gemini-web2api — нет).
    Возвращает (text, provider_name) — имя реально сработавшего провайдера,
    чтобы лог не врал про источник ответа."""
    if is_stopped():
        return None, None

    if image_path:
        # Канал картинок (seckция IMAGE из /models)
        provider = providers.get_provider_name(providers.IMAGE_URL)
        proxy_key = providers.PROXY_ENV_KEYS.get(provider.lower(), "")
        proxy = getattr(providers, proxy_key, "") if proxy_key else ""
        result = await _try_generate(
            providers.IMAGE_URL, providers.IMAGE_KEY, providers.IMAGE_MODEL,
            prompt, temperature, image_path, provider, attempt_limit=4, proxy=proxy
        )
        if result:
            return result, provider
        if providers.IMAGE_FALLBACK_URL:
            fb = providers.get_provider_name(providers.IMAGE_FALLBACK_URL)
            fb_proxy_key = providers.PROXY_ENV_KEYS.get(fb.lower(), "")
            fb_proxy = getattr(providers, fb_proxy_key, "") if fb_proxy_key else ""
            logger.info(f"🔄 [{provider}] Фолбек (картинка) → {fb}")
            result = await _try_generate(
                providers.IMAGE_FALLBACK_URL, providers.IMAGE_FALLBACK_KEY, providers.IMAGE_FALLBACK_MODEL,
                prompt, temperature, image_path, fb, attempt_limit=2, proxy=fb_proxy
            )
            if result:
                return result, fb
        logger.error(f"❌ Картинка: все провайдеры недоступны ({provider} + image-фолбек)")
        return None, provider

    provider = providers.get_provider_name(providers.MAIN_URL)
    # Определяем прокси по URL провайдера
    proxy_key = providers.PROXY_ENV_KEYS.get(provider.lower(), "")
    proxy = getattr(providers, proxy_key, "") if proxy_key else ""
    result = await _try_generate(
        providers.MAIN_URL, providers.MAIN_KEY, providers.MAIN_MODEL,
        prompt, temperature, image_path, provider, attempt_limit=4, proxy=proxy
    )
    if result:
        return result, provider

    # Фолбек
    if providers.MAIN_FALLBACK_URL:
        fb = providers.get_provider_name(providers.MAIN_FALLBACK_URL)
        fb_proxy_key = providers.PROXY_ENV_KEYS.get(fb.lower(), "")
        fb_proxy = getattr(providers, fb_proxy_key, "") if fb_proxy_key else ""
        logger.info(f"🔄 [{provider}] Фолбек → {fb}")
        result = await _try_generate(
            providers.MAIN_FALLBACK_URL, providers.MAIN_FALLBACK_KEY, providers.MAIN_FALLBACK_MODEL,
            prompt, temperature, image_path, fb, attempt_limit=2, proxy=fb_proxy
        )
        if result:
            return result, fb

    logger.error(f"❌ Все провайдеры недоступны ({provider} + фолбек)")
    return None, provider

async def _chat_completion(prompt, temperature=0.7, proxy_url=None, proxy_key=None, model=None, tag="tg_bot_chat", session_type=None):
    """Универсальный OpenAI-совместимый запрос (используется для рефлексии/выжимок)."""
    model = model or providers.SEARCH_MODEL
    proxy_url = proxy_url or providers.SEARCH_PROXY_URL
    proxy_key = proxy_key or providers.SEARCH_PROXY_KEY
    logger.info(f"📤 [{tag}:{model}] Отправка промпта длиной: {len(prompt)} символов")
    payload_dict = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": temperature
    }
    if session_type:
        payload_dict["session_type"] = session_type
    payload = json.dumps(payload_dict).encode('utf-8')

    req = urllib.request.Request(
        providers.chat_completions_url(proxy_url),
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {proxy_key}"
        }
    )
    _proxy = _proxy_for_url(proxy_url)

    for attempt in range(3):
        try:
            loop = asyncio.get_running_loop()
            resp = await loop.run_in_executor(None, lambda: _g4f_urlopen(req, timeout=180, proxy=_proxy))
            data = json.loads(resp.read().decode('utf-8'))
            text = data["choices"][0]["message"]["content"]
            logger.info(f"📤 [{tag}:{model}] Ответ получен, длина: {len(text)} символов")
            return text
        except Exception as e:
            logger.error(f"❌ [{tag}:{model}] Ошибка (попытка {attempt+1}): {e}")
            if attempt < 2:
                await asyncio.sleep(2)
    return None

async def _summary_fallback(payload_dict, tag="SUMMARY"):
    """Фолбек: сначала SUMMARY_FALLBACK_URL (если настроен), потом CHATGPT_FALLBACK."""
    # Приоритет: настроенный через /models → старый ChatGPT-фолбек
    fallback_url = providers.SUMMARY_FALLBACK_URL or providers.CHATGPT_FALLBACK_URL
    fallback_key = providers.SUMMARY_FALLBACK_KEY or providers.CHATGPT_FALLBACK_KEY
    fb_name = providers.get_provider_name(fallback_url)

    # Определяем прокси для фолбека по провайдеру
    fb_lower = fb_name.lower()
    proxy_for_fb_key = providers.PROXY_ENV_KEYS.get(fb_lower, "")
    proxy_for_fb = getattr(providers, proxy_for_fb_key, "") if proxy_for_fb_key else ""
    parsed_fb_proxy = providers.parse_proxy(proxy_for_fb) if proxy_for_fb else None

    try:
        req = urllib.request.Request(
            providers.chat_completions_url(fallback_url),
            data=json.dumps(payload_dict).encode('utf-8'),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {fallback_key}"
            }
        )
        loop = asyncio.get_running_loop()
        resp = await loop.run_in_executor(None, lambda: _g4f_urlopen(req, timeout=120, proxy=parsed_fb_proxy))
        data = json.loads(resp.read().decode('utf-8'))
        text = data["choices"][0]["message"]["content"].strip()
        logger.info(f"🆘 [{tag}:{fb_name}] Фолбек сработал: {len(text)} символов")
        return text
    except Exception as e:
        logger.error(f"❌ [{tag}:{fb_name}] Фолбек не сработал: {e}")
        return None

async def summarize_tool_output(cmd, purpose, output, state_manager):
    """Выжимка большого вывода команды через провайдер саммари (≤ TOOL_RESULT_LIMIT).
    Дипсику даём desc (зачем вызывали) + хвост переписки, чтобы выжимка была полезной."""
    if not output:
        return ""
    if len(output) > config.SUMMARY_INPUT_LIMIT:
        output = output[:config.SUMMARY_INPUT_LIMIT] + f"\n…(обрезано, всего {len(output)} симв.)"
    recent = state_manager.state["chat_history"][-10:]
    recent_text = "\n".join([f"[{m.get('ts','')}] {render_role(m['role'])}: {m['content']}" for m in recent])

    system_prompt = (
        "Ты — модуль выжимки вывода команды/тула для чат-бота. "
        "Тебе дают: команду, ЦЕЛЬ (зачем выполнялась — бот обычно пишет, какой элемент ему нужен, "
        "например «кнопка Создать репозиторий»), хвост диалога (контекст) и полный вывод команды. "
        "Сделай ОЧЕНЬ короткую выжимку вывода, максимально полезную именно для заявленной цели. "
        "\n\nВАЖНО ПРО ЭЛЕМЕНТЫ (ref): если вывод содержит строки вида «e5: button \"Создать\"» "
        "(ref, затем тип и имя через двоеточие) — это кликабельные элементы страницы. "
        "Если ЦЕЛЬ описывает конкретный нужный элемент — верни ТОЛЬКО строку(и) с этим ref, "
        "не весь список. Пример: цель «кнопка Создать» → «e34: button \"Create repository\"». "
        "Если под цель попадает несколько элементов — верни их все, но только их. "
        "Если ЦЕЛЬ не про конкретный элемент (или таких строк в выводе нет) — обычная выжимка по цели. "
        f"ЖЁСТКИЙ ЛИМИТ: максимум {config.TOOL_RESULT_LIMIT} символов. "
        "Формат: 1-3 коротких предложения. Без вводных слов и пояснений, только суть выжимки."
    )

    user_prompt = (
        f"КОМАНДА: {cmd}\n"
        f"ЗАЧЕМ ВЫПОЛНЯЛАСЬ: {purpose if purpose else '(не указано)'}\n\n"
        f"ПОСЛЕДНИЙ КОНТЕКСТ ДИАЛОГА:\n{recent_text}\n\n"
        f"--- ПОЛНЫЙ ВЫВОД ---\n{output}"
    )

    payload_dict = {
        "model": providers.SUMMARY_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ],
        "temperature": 0.3,
        "user": "tg_bot_tool_summary"
    }

    req = urllib.request.Request(
        providers.chat_completions_url(providers.SUMMARY_PROXY_URL),
        data=json.dumps(payload_dict).encode('utf-8'),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {providers.SUMMARY_PROXY_KEY}"
        }
    )
    _summary_proxy = _proxy_for_url(providers.SUMMARY_PROXY_URL)

    for attempt in range(2):
        try:
            loop = asyncio.get_running_loop()
            resp = await loop.run_in_executor(None, lambda: _g4f_urlopen(req, timeout=60, proxy=_summary_proxy))
            data = json.loads(resp.read().decode('utf-8'))
            text = data["choices"][0]["message"]["content"].strip()
            logger.info(f"✂️ [SUMMARY] Выжимка вывода: {len(text)} символов")
            return text[:config.TOOL_RESULT_LIMIT]
        except Exception as e:
            logger.error(f"❌ [SUMMARY] Ошибка выжимки (попытка {attempt+1}): {e}")
            if attempt < 1:
                await asyncio.sleep(2)
    # Фолбек на настроенный провайдер (SUMMARY_FALLBACK_URL / CHATGPT_FALLBACK_URL)
    fb = await _summary_fallback(payload_dict, "SUMMARY")
    if fb:
        return fb[:config.TOOL_RESULT_LIMIT]
    # Финальный фолбек при полном отказе — режем жадно
    return output[:config.TOOL_RESULT_LIMIT] + f"\n... (всего {len(output)} симв.)"

async def summarize_search_result(search_query, output, state_manager):
    """Выжимка большого поискового результата через провайдер саммари (≤ TOOL_RESULT_LIMIT).
    Дипсику передаём оригинальный поисковый запрос + хвост переписки."""
    if not output:
        return ""
    if len(output) > config.SUMMARY_INPUT_LIMIT:
        output = output[:config.SUMMARY_INPUT_LIMIT] + f"\n…(обрезано, всего {len(output)} симв.)"
    recent = state_manager.state["chat_history"][-10:]
    recent_text = "\n".join([f"[{m.get('ts','')}] {render_role(m['role'])}: {m['content']}" for m in recent])

    system_prompt = (
        "Ты — модуль выжимки результатов веб-поиска для чат-бота. "
        "Тебе дают: оригинальный поисковый запрос, хвост диалога (контекст) и полный результат поиска. "
        "Сделай ОЧЕНЬ короткую выжимку, максимально полезную именно для этого запроса. "
        "Не упускай ничего, что относится к запросу (факты, цифры, ссылки, имена). "
        f"ЖЁСТКИЙ ЛИМИТ: максимум {config.TOOL_RESULT_LIMIT} символов. "
        "Формат: 1-3 коротких предложения. Без вводных слов и пояснений, только суть выжимки."
    )

    user_prompt = (
        f"ПОИСКОВЫЙ ЗАПРОС: {search_query}\n\n"
        f"ПОСЛЕДНИЙ КОНТЕКСТ ДИАЛОГА:\n{recent_text}\n\n"
        f"--- ПОЛНЫЙ РЕЗУЛЬТАТ ПОИСКА ---\n{output}"
    )

    payload_dict = {
        "model": providers.SUMMARY_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ],
        "temperature": 0.3,
        "user": "tg_bot_search_summary"
    }

    req = urllib.request.Request(
        providers.chat_completions_url(providers.SUMMARY_PROXY_URL),
        data=json.dumps(payload_dict).encode('utf-8'),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {providers.SUMMARY_PROXY_KEY}"
        }
    )
    _summary_proxy = _proxy_for_url(providers.SUMMARY_PROXY_URL)

    for attempt in range(2):
        try:
            loop = asyncio.get_running_loop()
            resp = await loop.run_in_executor(None, lambda: _g4f_urlopen(req, timeout=60, proxy=_summary_proxy))
            data = json.loads(resp.read().decode('utf-8'))
            text = data["choices"][0]["message"]["content"].strip()
            logger.info(f"✂️ [SUMMARY] Выжимка поиска: {len(text)} символов")
            return text[:config.TOOL_RESULT_LIMIT]
        except Exception as e:
            logger.error(f"❌ [SUMMARY] Ошибка выжимки поиска (попытка {attempt+1}): {e}")
            if attempt < 1:
                await asyncio.sleep(2)
    # Фолбек на настроенный провайдер (SUMMARY_FALLBACK_URL / CHATGPT_FALLBACK_URL)
    fb = await _summary_fallback(payload_dict, "SUMMARY-SEARCH")
    if fb:
        return fb[:config.TOOL_RESULT_LIMIT]
    # Финальный фолбек при полном отказе — режем жадно
    return output[:config.TOOL_RESULT_LIMIT] + f"\n... (всего {len(output)} симв.)"

def perplexity_search(query):
    if not providers.PERPLEXITY_COOKIE or not providers.PERPLEXITY_RW_TOKEN:
        return None
    import uuid as _uuid
    params = {
        "last_backend_uuid": str(_uuid.uuid4()),
        "frontend_uuid": str(_uuid.uuid4()),
        "read_write_token": providers.PERPLEXITY_RW_TOKEN,
        "query_source": "user",
        "source": "default",
        "mode": "copilot",
        "model_preference": "turbo",
        "search_focus": "internet",
        "sources": ["web"],
        "use_schematized_api": True,
        "version": "2.18",
        "supports_tool_approval_modal": True,
        "is_related_query": False,
        "language": "ru-RU",
    }
    body = json.dumps({"params": params, "query_str": query}, ensure_ascii=False).encode('utf-8')
    req = urllib.request.Request(
        "https://www.perplexity.ai/rest/sse/perplexity_ask",
        data=body,
        headers={
            "Content-Type": "application/json",
            "Accept": "text/event-stream, application/json",
            "Origin": "https://www.perplexity.ai",
            "Referer": "https://www.perplexity.ai/",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36",
            "Cookie": providers.PERPLEXITY_COOKIE,
        },
    )
    try:
        resp = providers.open_url(req, timeout=90)
        chunk = resp.read().decode('utf-8', 'replace')
    except Exception as e:
        logger.error(f"❌ [PERPLEXITY] запрос упал: {e}")
        return None
    parts = []
    for line in chunk.splitlines():
        line = line.strip()
        if not line.startswith('data:'):
            continue
        try:
            ev = json.loads(line[5:].strip())
        except Exception:
            continue
        if not isinstance(ev, dict):
            continue
        if ev.get('type') == 'answer':
            c = ev.get('content')
            if isinstance(c, dict) and c.get('text'):
                parts.append(c['text'])
    out = ' '.join(parts).strip()
    if out:
        logger.info(f"🔍 [PERPLEXITY] фолбек-поиск: {len(out)} символов")
    return out or None

def _g4f_search_cleanup(text):
    """Убирает авто-цитаты g4f-Gemini ([[N]](url)/[[N]]([url](url)) и схлопывает
    склеенные черновик+финал. Markdown-ссылки вида [url](url), где текст==адрес,
    разворачиваются в голый URL. Блок источников внизу НЕ трогается."""
    text = re.sub(r'\[\[\d+\]\]\(\[[^\]]*\]\([^)]*\)', '', text)
    text = re.sub(r'\[\[\d+\]\]\([^)]*\)', '', text)
    text = re.sub(r'\[\[\d+\]\]', '', text)
    # [url](url) -> url (текст ссылки совпадает с адресом)
    text = re.sub(r'\[([^\]]*)\]\((\1)\)', r'\1', text)
    # если первая половина абзацев == второй (дубль черновик+финал) — оставляем одну
    paras = [p for p in text.split('\n\n') if p.strip()]
    n = len(paras)
    for i in range(n // 2, 0, -1):
        if paras[:i] == paras[i:2 * i]:
            paras = paras[:i]
            break
    text = '\n\n'.join(paras)
    # если g4f склеил черновик+финал ВСТЫК: заголовок текста встречается дважды,
    # берём с начала последнего вхождения; если после среза остаются только
    # источники (ответ потерялся) — откатываемся к полному тексту
    head = text[:60]
    if head:
        pos = -1
        start = 0
        while True:
            hit = text.find(head, start)
            if hit == -1:
                break
            pos = hit
            start = hit + 1
        if pos > 0:
            candidate = text[pos:]
            body = re.split(r'\n\s*>\s*\[0\]', candidate)[0]
            if len(body.strip()) >= 40:
                text = candidate
    text = re.sub(r'\s+([.,;:!?])', r'\1', text).strip()
    return text.strip()


async def search_web_g4f(query):
    """Фолбек-поиск через g4f.space /api/Gemini (реальный gemini.google.com
    на чужих аккаунтах) с web_search=True. Кредиты — через PROXY_G4F (тот же
    IP, что у бейкера). Ретраи при 429/сбоях. Возвращает текст или None."""
    payload = json.dumps({
        "model": "gemini-2.5-flash",
        "messages": [{"role": "user", "content": f"Найди в интернете и ответь БЕЗ лишних слов: {query}\nЕсли просят прямую ссылку — ОБЯЗАТЕЛЬНО возьми URL из найденного источника (проверенного в поиске), не придумывай его.\nОтвечай ТОЛЬКО обычным текстом, без markdown и разметки. Ссылки пиши голым URL, ровно как они есть в источнике."}],
        "temperature": 0.3,
        "web_search": True
    }).encode('utf-8')
    url = "https://g4f.space/api/Gemini/chat/completions"

    # Прокси g4f
    g4f_proxy = providers.parse_proxy(providers.PROXY_G4F) if providers.PROXY_G4F else None

    for attempt in range(2):
        try:
            req = urllib.request.Request(
                url,
                data=payload,
                headers={"Content-Type": "application/json"}
            )
            loop = asyncio.get_running_loop()
            resp = await loop.run_in_executor(None, lambda: _g4f_urlopen(req, timeout=150, proxy=g4f_proxy))
            data = json.loads(resp.read().decode('utf-8'))
            text = data["choices"][0]["message"]["content"]
            if text:
                text = _g4f_search_cleanup(text)
                logger.info(f"🔍 [SEARCH-G4F] Результат: {len(text)} символов")
                return text
        except Exception as e:
            logger.error(f"❌ [SEARCH-G4F] Ошибка (попытка {attempt+1}): {e}")
            if attempt < 1:
                await asyncio.sleep(3)
    return None

async def search_web(query):
    _sn = providers.get_provider_name(providers.SEARCH_PROXY_URL)
    logger.info(f"🔍 [SEARCH-{_sn}] Поиск: {query}")
    payload = json.dumps({
        "model": providers.SEARCH_MODEL,
        "messages": [
            {"role": "user", "content": query}
        ],
        "temperature": 0.3,
        "search": True,
        "user": "tg_bot_search"
    }).encode('utf-8')

    req = urllib.request.Request(
        providers.chat_completions_url(providers.SEARCH_PROXY_URL),
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {providers.SEARCH_PROXY_KEY}"
        }
    )

    _search_proxy = _proxy_for_url(providers.SEARCH_PROXY_URL)

    for attempt in range(2):
        try:
            loop = asyncio.get_running_loop()
            resp = await loop.run_in_executor(None, lambda: _g4f_urlopen(req, timeout=60, proxy=_search_proxy))
            data = json.loads(resp.read().decode('utf-8'))
            text = data["choices"][0]["message"]["content"]
            text = re.sub(r'\[citation:\d+\]', '', text)
            text = re.sub(r'\s+([.,;:!?])', r'\1', text).strip()
            logger.info(f"✅🔍 [SEARCH-{_sn}] Результат: {len(text)} символов")
            return text
        except Exception as e:
            logger.error(f"❌🔍 [SEARCH-{_sn}] Ошибка (попытка {attempt+1}): {e}")
            if attempt < 1:
                await asyncio.sleep(2)

    # Фолбек: настроенный через /models
    if providers.SEARCH_FALLBACK_URL:
        fb_name = providers.get_provider_name(providers.SEARCH_FALLBACK_URL)
        logger.info(f"🔄 [SEARCH] Фолбек → {fb_name}")
        try:
            fb_payload = json.dumps({
                "model": providers.SEARCH_FALLBACK_MODEL,
                "messages": [{"role": "user", "content": query}],
                "temperature": 0.3,
            }).encode('utf-8')
            fb_req = urllib.request.Request(
                providers.chat_completions_url(providers.SEARCH_FALLBACK_URL),
                data=fb_payload,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {providers.SEARCH_FALLBACK_KEY}"
                }
            )
            loop = asyncio.get_running_loop()
            # Прокси фолбека — по провайдеру фолбека (как у основного)
            fb_proxy_key = providers.PROXY_ENV_KEYS.get(fb_name.lower(), "")
            fb_proxy_str = getattr(providers, fb_proxy_key, "") if fb_proxy_key else ""
            fb_proxy = providers.parse_proxy(fb_proxy_str) if fb_proxy_str else None
            resp = await loop.run_in_executor(None, lambda: _g4f_urlopen(fb_req, timeout=60, proxy=fb_proxy))
            data = json.loads(resp.read().decode('utf-8'))
            text = data["choices"][0]["message"]["content"]
            logger.info(f"🔍 [SEARCH:{fb_name}] Фолбек-результат: {len(text)} символов")
            return text
        except Exception as e:
            logger.error(f"❌ [SEARCH:{fb_name}] Фолбек-ошибка: {e}")

    # Фолбек: g4f.space
    try:
        g4f_result = await search_web_g4f(query)
        if g4f_result:
            return g4f_result
        logger.error("❌ [SEARCH-G4F] фолбек вернул пусто")
    except Exception as e:
        logger.error(f"❌ [SEARCH-G4F] фолбек-ошибка: {e}")
    return None

_transcribe_key_idx = 0

async def transcribe_voice(audio_bytes, filename):
    global _transcribe_key_idx

    # Ключ: TRANSCRIBE_KEY из .env, иначе ротация legacy GROQ_KEYS
    if providers.TRANSCRIBE_KEY:
        key = providers.TRANSCRIBE_KEY
    elif not config.GROQ_KEYS:
        logger.error("❌ [TRANSCRIBE] Нет ключей (TRANSCRIBE_KEY / GROQ_KEYS)")
        return None
    else:
        key = config.GROQ_KEYS[_transcribe_key_idx % len(config.GROQ_KEYS)]
        _transcribe_key_idx += 1

    text = await _transcribe_url(audio_bytes, filename,
                                  providers.TRANSCRIBE_URL, key, providers.TRANSCRIBE_MODEL)
    if text:
        return text

    if providers.TRANSCRIBE_FALLBACK_URL:
        fb_key = providers.TRANSCRIBE_FALLBACK_KEY or key
        fb_model = providers.TRANSCRIBE_FALLBACK_MODEL or providers.TRANSCRIBE_MODEL
        logger.info("🔄 [TRANSCRIBE] Фолбек → %s", providers.get_provider_name(providers.TRANSCRIBE_FALLBACK_URL))
        text = await _transcribe_url(audio_bytes, filename,
                                      providers.TRANSCRIBE_FALLBACK_URL, fb_key, fb_model)
        if text:
            return text

    logger.error("❌ [TRANSCRIBE] Все провайдеры недоступны")
    return None

async def _transcribe_url(audio_bytes, filename, url, key, model, attempts=2):
    """Один запрос транскрибации (multipart). Прокси — по URL провайдера. Возвращает текст или None."""
    if not url or not key:
        logger.warning(f"⚠️ [TRANSCRIBE] Нет URL/ключа")
        return None
    proxy = providers.proxy_for_url(url)
    import io
    import requests as req
    loop = asyncio.get_running_loop()
    for attempt in range(attempts):
        if is_stopped():
            return None
        try:
            proxies = {"https": proxy, "http": proxy} if proxy else None
            def do_request():
                return req.post(
                    providers.transcriptions_url(url),
                    headers={"Authorization": f"Bearer {key}"},
                    data={"model": model, "language": "ru"},
                    files={"file": (filename, io.BytesIO(audio_bytes), "audio/ogg")},
                    timeout=30,
                    proxies=proxies,
                )
            resp = await loop.run_in_executor(None, do_request)
            logger.info(f"🎤 [TRANSCRIBE] ответ {resp.status_code}: {resp.text[:300]}")
            if resp.status_code != 200:
                raise Exception(f"HTTP {resp.status_code}")
            data = resp.json()
            text = data.get('text', '')
            if not text:
                raise Exception("пустой ответ")
            logger.info(f"🎤 [TRANSCRIBE] Распознано: {text[:100]}")
            return text
        except Exception as e:
            logger.error(f"❌ [TRANSCRIBE] Ошибка (попытка {attempt+1}): {e}")
            if attempt < attempts - 1:
                await asyncio.sleep(2)
    return None

def _escape_raw_newlines_in_json_strings(text):
    """Экранирует живые переносы строк внутри JSON-строк.

    Модели часто вставляют код в строку replies с реальными переводами строки
    (не экранированным \\n) — такой ответ валиден как текст, но не как JSON.
    Проходим по строке от первого '{', отслеживая состояние "внутри JSON-строки",
    и заменяем реальные \r\n внутри строк на экранированную \\n.
    """
    start = text.find('{')
    if start == -1:
        return text
    out = list(text[:start])
    in_str = False
    esc = False
    for ch in text[start:]:
        if in_str:
            if esc:
                esc = False
            elif ch == '\\':
                esc = True
            elif ch == '"':
                in_str = False
            elif ch in '\r\n':
                out.append('\\n')
                continue
        else:
            if ch == '"':
                in_str = True
        out.append(ch)
    if in_str:
        out.append('"')
    return ''.join(out)


def _repair_bad_json_escapes(text):
    """Чинит невалидные JSON-escape-последовательности внутри строк.

    Модели часто вставляют в ответ одиночный backslash не по правилам JSON:
    ``\\имя_юзера``, ``C:\\Users\\...``, ``1\\2``. Спецификация JSON допускает
    только ``\\" \\\\ \\/ \\b \\f \\n \\r \\t \\uXXXX``, поэтому ``\\и`` — это
    ошибка, которая валит парсинг всего ответа. Здесь каждый backslash перед
    символом, который не является валидным escape, удваивается (``\\и`` →
    ``\\\\и``) — парсер вернёт обычный литеральный ``\\и``.
    """
    start = text.find('{')
    if start == -1:
        return text
    out = list(text[:start])
    in_str = False
    esc = False
    i = start
    s = text
    n = len(s)
    while i < n:
        ch = s[i]
        if in_str:
            if esc:
                esc = False
                if ch not in '"\\/bfnrtu':
                    out.append('\\')
                out.append(ch)
                i += 1
                continue
            if ch == '\\':
                esc = True
                out.append(ch)
                i += 1
                continue
            if ch == '"':
                in_str = False
            out.append(ch)
            i += 1
            continue
        if ch == '"':
            in_str = True
        out.append(ch)
        i += 1
    if in_str:
        out.append('"')
    return ''.join(out)


def _extract_embedded_json(raw_text):
    """Ищет внутри текста валидный JSON-объект.

    Помогает, когда модель "намалевала" мусор вокруг ответа или вложила JSON
    в JSON (битый внешний объект + живой внутренний). Перебирает позиции '{',
    собирает сбалансированные {…}-фрагменты (с учётом строк и экранов) и парсит
    их. Возвращает первый валидный объект с ключом replies, иначе первый
    валидный объект, иначе None.
    """
    best = None
    n = len(raw_text)
    i = 0
    while i < n:
        start = raw_text.find('{', i)
        if start == -1:
            break
        depth = 0
        in_str = False
        esc = False
        j = start
        while j < n:
            ch = raw_text[j]
            if in_str:
                if esc:
                    esc = False
                elif ch == '\\':
                    esc = True
                elif ch == '"':
                    in_str = False
            elif ch == '"':
                in_str = True
            elif ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0:
                    cand = raw_text[start:j + 1]
                    try:
                        obj = json.loads(cand)
                    except Exception:
                        break
                    if isinstance(obj, dict):
                        if obj.get("replies"):
                            return obj
                        if best is None:
                            best = obj
                    break
            j += 1
        i = start + 1
    return best


async def try_parse_or_repair_json(raw_text):
    if not raw_text:
        return None

    # Логируем сырой ответ в файл
    try:
        with open(str(config.BASE_DIR / "last_raw_response.txt"), "w") as f:
            f.write(f"=== RAW RESPONSE ({len(raw_text)} симв) ===\n")
            f.write(raw_text[:5000])
            f.write(f"\n=== END ===\n")
    except Exception:
        pass

    try:
        return json.loads(clean_json_response(raw_text))
    except (json.JSONDecodeError, AttributeError, ValueError):
        # Модель могла вложить код в строку с реальными переносами — чиним их
        try:
            return json.loads(_escape_raw_newlines_in_json_strings(raw_text))
        except (json.JSONDecodeError, AttributeError, ValueError):
            # Модель могла вставить одиночный backslash (C:\Users, \имя_юзера) —
            # чиним невалидные escape-последовательности, потом переносы строк
            try:
                return json.loads(
                    _escape_raw_newlines_in_json_strings(
                        _repair_bad_json_escapes(raw_text)
                    )
                )
            except (json.JSONDecodeError, AttributeError, ValueError):
                # Модель "намалевала" мусор (битый/вложенный JSON). Пытаемся вытащить
                # валидный объект из текста и отправить только его replies,
                # остальное выбрасываем.
                embedded = _extract_embedded_json(raw_text)
                if embedded and embedded.get("replies"):
                    logger.warning(f"⚠️ [JSON] Ответ-мусор: извлечён вложенный JSON, шлю только replies, остальное выброшено.")
                    return embedded
                # JSON-подобный мусор без извлекаемых replies — НЕ шлём, молчим
                if raw_text.lstrip().startswith(("{", "[")):
                    logger.warning(f"⚠️ [JSON] Битый JSON без replies — мусор выброшен, бот молчит.")
                    return {"replies": [], "mood_shift": 0.0}
                # Обычный текст без JSON — оборачиваем и отправляем как есть
                logger.warning(f"⚠️ [JSON] Ответ без JSON-формата, отправляю как текст.")
                text = raw_text.strip()
                if text:
                    return {"replies": [text], "mood_shift": 0.0}
    return None

async def retrieve_memory(user_query, query_topic, state_manager):
    logger.info(f"🧠 [MEMORY] ВСПОМИНАЮ: {query_topic}")
    
    r1 = r2 = ""
    if query_topic:
        r1, r2 = await asyncio.gather(
            query_rag(user_query, top_k=5),
            query_rag(query_topic, top_k=5),
        )
        r1 = r1 or ""
        r2 = r2 or ""
    result = "\n---\n".join(x for x in (r1, r2) if x)
    if result:
        # LLM-шаг: формируем ответ для бота по сырым фрагментам
        formatted = await format_memory_answer(user_query, result)
        if formatted:
            result = formatted

    if result:
        # Запись в историю: короткий результат — целиком, длинный — только факт "вспоминал"
        if len(result) <= config.TOOL_RESULT_LIMIT:
            rec = f'🧠 вспоминал: "{query_topic}" →\n{result}'
        else:
            rec = f'🧠 вспоминал: "{query_topic}" (результат большой, без текста)'
        await state_manager.add_tool_record(rec)

        memory_context = f"ВНИМАНИЕ! Это приоритетная задача. Пользователь просит тебя что-то вспомнить. Вот что нашла система памяти:\n---\n{result}\n---\nТвоя задача — ответить на вопрос: '{user_query}'. Используй найденные данные. Следуй своему характеру. Если ответа нет, честно признайся."
        if len(result) > config.TOOL_RESULT_LIMIT:
            memory_context += "\n\nПРИМЕЧАНИЕ: результат воспоминания оказался длинным — отвечай пользователю КОРОТКО, по существу."
    else:
        memory_context = f"Пользователь спрашивает: '{user_query}'. Память пуста или произошла ошибка. Если ответа нет, честно признайся."
    
    return await process_user_input(user_query, state_manager, memory_context=memory_context)

async def update_longterm_summary(state_manager):
    """Обновляет долгосрочное саммари через настроенный провайдер (SUMMARY_*) каждые 50 сообщений."""
    logger.info(f"📌 [SUMMARY] Запуск обновления саммари (messages_since_summary={state_manager.state.get('messages_since_summary', 0)})")
    summary_prov = providers.get_provider_name(providers.SUMMARY_PROXY_URL)

    # Последние 50 сообщений диалога
    recent = state_manager.state["chat_history"][-config.CHAT_HISTORY_LIMIT:]
    if not recent:
        logger.warning("📌 [SUMMARY] Нет сообщений для саммари")
        return

    recent_text = "\n".join([f"[{m.get('ts','')}] {render_role(m['role'])}: {m['content']}" for m in recent])

    # Пакетное извлечение фактов из окна (вместе с триггером саммари)
    window_messages = [
        f"[{m.get('ts', '')}] {'Пользователь' if m['role'] == 'user' else 'Бот'}: {m['content']}"
        for m in recent if m.get('content')
    ]
    if window_messages:
        await remember_window(window_messages)

    prev_summary = state_manager.state.get("summary", "")
    interests = state_manager.state.get("interests", [])
    interests_text = "\n".join([f"- {t['id']}: {t['text']}" for t in interests]) if interests else "(нет)"

    system_prompt = (
        "Ты —  друг пользователя. Тебе дадут: (1) прошлое саммари разговора (может быть пустым), "
        "(2) новые 50 сообщений вашего диалога, (3) список ИНТЕРЕСОВ (темы, которые ты хотел поднять).\n\n"
        "ЗАДАЧА 1 — обнови саммари:\n"
        "- СОХРАНИ всю важную инфу из прошлого саммари: имена, ваши отношения, важные события, решения, договорённости, планы, факты о пользователе.\n"
        "- ДОБАВЬ новые важные факты из новых сообщений.\n"
        "- ПИШИ  СЖАТО И ПЛОТНО, НЕ ТЕРЯЯ СМЫСЛ. ЖЁСТКИЙ ЛИМИТ: максимум 700 символов.\n"
        "- Не расширяй саммари без причины: если в новых сообщениях нет НОВОГО важного — верни прежний текст почти без изменений, только подправь устаревшее.\n"
        "- Без воды, оценок, эпитетов, вводных фраз. Только суть: факты, решения, имена, договорённости.\n"
        "- ЗАПРЕЩЕНО писать в саммари технические детали, пароли, секреты, логины, токены, ключи, IP/порты и прочую чувствительную инфу — саммари это только память о контексте и отношениях, а не хранилище секретов.\n"
        "- Если часть старого саммари устарела/опровергнута — замени её, не дублируй.\n"
        "- Приоритет: что важно помнить ДОЛГО.\n"
        "- ПИШИ ОТ ПЕРВОГО ЛИЦА: 'я', 'мне', 'мне показалось', 'я решил', 'я предложил'. Пример: 'Петя сказал X, я Y', 'Мы решили Z', 'Петя обиделся на меня за X', 'Я слелал X, но у меня не получилось'. Никаких упоминаний 'бот', 'модель', 'ассистент', 'ИИ', 'система'. Петя - пользователь. Ты - ео собеседник.\n"
        "- НЕ включай в саммари данные о будильниках, напоминаниях, таймерах, алармах — они динамические, меняются и только мешают.\n"
        "- НЕ записывай рутину и микро-события дня: напоминания про чай/дела, тесты функций, авторасшифровку голосовых, «напомнил/сработало/не сработало», просьбы повторить. Это однодневный мусор, в долгую память он не нужен.\n"
        "- Держи структуру саммари короткими смысловыми блоками: (1) Я и пользователь (кто я, кто пользователь, наши отношения, важные детали, без технической ерунды), (2) важные события и разговоры, (3) работа и планы. Не разноси в длинные абзацы.\n\n"
        "ЗАДАЧА 2 — проверь ИНТЕРЕСЫ (темы из списка ниже): определи, какие из них ЯВНО уже выполнены в этих 50 сообщениях — "
        "когда тему начал Ты САМ, без запроса пользователя (сам начал разговор про неё, сам задал вопрос, сам предложил/напомнил). "
        "Помечай как выполненную ТОЛЬКО такие темы. Если тема лишь упоминалась вскользь или её поднял сам пользователь — НЕ помечай. Не надумывай, никаких ложных срабатываний.\n\n"
        "СТРОГОЕ ПРАВИЛО ФОРМАТА: Ты ОБЯЗАН вернуть ТОЛЬКО JSON ровно в этом формате:\n"
        '{"summary": "текст саммари", "done_interests": ["id1", "id2"]}\n'
        "Никакого другого JSON. Никаких мыслей, обсуждений, действий — ТОЛЬКО summary и done_interests.\n"
        "Если.done_interests пуст — верни пустой массив: \"done_interests\": []\n"
        "Если поле summary отсутствует или содержит thoughts/discussions/actions — ответ СЧИТАЕТСЯ ОШИБКОЙ."
    )

    user_prompt = (
        f"ПРОШЛОЕ САММАРИ:\n{prev_summary if prev_summary else '(пусто)'}\n\n"
        f"---\n\nНОВЫЕ СООБЩЕНИЯ:\n{recent_text}\n\n"
        f"---\n\nИНТЕРЕСЫ (темы, которые ты хотел поднять):\n{interests_text}"
    )

    payload_dict = {
        "model": providers.SUMMARY_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ],
        "temperature": 0.3,
        "user": "tg_bot_summary",
        "session_type": "summary"
    }

# --- ОСНОВНОЙ канал: провайдер саммари (SUMMARY_PROXY_URL) ---
    _summary_proxy = _proxy_for_url(providers.SUMMARY_PROXY_URL)
    # session_type не нужен всем провайдерам — убираем перед отправкой
    chatgpt_payload = dict(payload_dict)
    chatgpt_payload.pop("session_type", None)

    # Сначала пробуем основной провайдер напрямую
    try:
        req = urllib.request.Request(
            providers.chat_completions_url(providers.SUMMARY_PROXY_URL),
            data=json.dumps(chatgpt_payload).encode('utf-8'),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {providers.SUMMARY_PROXY_KEY}"
            }
        )
        loop = asyncio.get_running_loop()
        resp = await loop.run_in_executor(None, lambda: _g4f_urlopen(req, timeout=120, proxy=_summary_proxy))
        data = json.loads(resp.read().decode('utf-8'))
        fb = data["choices"][0]["message"]["content"].strip()
        logger.info(f"📌 [SUMMARY:{summary_prov}] Основной канал ответил: {len(fb)} символов")
    except Exception as e:
        logger.error(f"❌ [SUMMARY:{summary_prov}] Основной канал не сработал: {e}")
        fb = None

    if not fb:
        fb = await _summary_fallback(chatgpt_payload, "SUMMARY")
    if fb:
        try:
            parsed = json.loads(clean_json_response(fb))
            if isinstance(parsed.get("summary"), str) and not parsed.get("thoughts"):
                summary_text = parsed["summary"].strip()
                legit_ids = {t["id"] for t in interests}
                done = [x for x in (parsed.get("done_interests") or []) if x in legit_ids]
                await state_manager.set_summary(summary_text)
                if done:
                    await state_manager.remove_interests(done)
                return summary_text
            logger.warning(f"⚠️ [SUMMARY:{summary_prov}] Вернул невалидный формат, попытка через основной канал (2-й проход)")
        except (json.JSONDecodeError, AttributeError):
            # Не JSON — но это может быть чистый саммари-текст, пробуем ещё раз
            logger.warning(f"⚠️ [SUMMARY:{summary_prov}] Вернул не JSON, попытка через основной канал (2-й проход)")

    # --- Повтор через основной канал (с session_type, для совместимости с сессионными провайдерами) ---
    req = urllib.request.Request(
        providers.chat_completions_url(providers.SUMMARY_PROXY_URL),
        data=json.dumps(payload_dict).encode('utf-8'),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {providers.SUMMARY_PROXY_KEY}"
        }
    )
    for attempt in range(2):
        try:
            loop = asyncio.get_running_loop()
            resp = await loop.run_in_executor(None, lambda: _g4f_urlopen(req, timeout=60, proxy=_summary_proxy))
            data = json.loads(resp.read().decode('utf-8'))
            text = data["choices"][0]["message"]["content"].strip()
            logger.info(f"📌 [SUMMARY:{summary_prov}] Ответ получен: {len(text)} символов")
            try:
                parsed = json.loads(clean_json_response(text))
                if isinstance(parsed.get("summary"), str) and not parsed.get("thoughts"):
                    summary_text = parsed["summary"].strip()
                    legit_ids = {t["id"] for t in interests}
                    done = [x for x in (parsed.get("done_interests") or []) if x in legit_ids]
                    await state_manager.set_summary(summary_text)
                    if done:
                        await state_manager.remove_interests(done)
                    return summary_text
                logger.warning(f"⚠️ [SUMMARY:{summary_prov}] Невалидный формат (попытка {attempt+1})")
                if attempt < 1:
                    await asyncio.sleep(2)
                else:
                    await state_manager.set_summary(text)
                    return text
            except (json.JSONDecodeError, AttributeError):
                logger.warning(f"⚠️ [SUMMARY:{summary_prov}] Ответ не JSON — сохраняю как есть")
                await state_manager.set_summary(text)
                return text
        except Exception as e:
            logger.error(f"❌ [SUMMARY:{summary_prov}] Ошибка (попытка {attempt+1}): {e}")
            if attempt < 1:
                await asyncio.sleep(2)
    return None

async def _reflection_fallback(payload_dict):
    """Фолбек рефлексии: сначала основной провайдер, потом fallback."""
    # Основной провайдер
    _refl_proxy = _proxy_for_url(providers.REFLECTION_PROXY_URL)
    try:
        req = urllib.request.Request(
            providers.chat_completions_url(providers.REFLECTION_PROXY_URL),
            data=json.dumps(payload_dict).encode('utf-8'),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {providers.REFLECTION_PROXY_KEY}"
            }
        )
        loop = asyncio.get_running_loop()
        resp = await loop.run_in_executor(None, lambda: _g4f_urlopen(req, timeout=120, proxy=_refl_proxy))
        data = json.loads(resp.read().decode('utf-8'))
        text = data["choices"][0]["message"]["content"].strip()
        logger.info(f"🆘 [REFLECTION:{providers.REFLECTION_MODEL}] Основной сработал: {len(text)} символов")
        return text
    except Exception as e:
        logger.error(f"❌ [REFLECTION:{providers.REFLECTION_MODEL}] Основной не сработал: {e}")

    # Фолбек
    fallback_url = providers.REFLECTION_FALLBACK_URL or providers.SUMMARY_FALLBACK_URL or providers.CHATGPT_FALLBACK_URL
    fallback_key = providers.REFLECTION_FALLBACK_KEY or providers.SUMMARY_FALLBACK_KEY or providers.CHATGPT_FALLBACK_KEY
    fb_name = providers.get_provider_name(fallback_url)

    # Прокси для фолбека по провайдеру
    fb_lower2 = fb_name.lower()
    fb_proxy_key2 = providers.PROXY_ENV_KEYS.get(fb_lower2, "")
    fb_proxy_str2 = getattr(providers, fb_proxy_key2, "") if fb_proxy_key2 else ""
    parsed_fb_proxy2 = providers.parse_proxy(fb_proxy_str2) if fb_proxy_str2 else None

    try:
        req = urllib.request.Request(
            providers.chat_completions_url(fallback_url),
            data=json.dumps(payload_dict).encode('utf-8'),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {fallback_key}"
            }
        )
        loop = asyncio.get_running_loop()
        resp = await loop.run_in_executor(None, lambda: _g4f_urlopen(req, timeout=120, proxy=parsed_fb_proxy2))
        data = json.loads(resp.read().decode('utf-8'))
        text = data["choices"][0]["message"]["content"].strip()
        logger.info(f"🆘 [REFLECTION:{fb_name}] Фолбек сработал: {len(text)} символов")
        return text
    except Exception as e:
        logger.error(f"❌ [REFLECTION:{fb_name}] Фолбек не сработал: {e}")
        return None


async def generate_reflection(state_manager):
    logger.info("💡 [REFLECTION] Запускаю процесс гибридной рефлексии...")
    recent_history = state_manager.state["chat_history"][-40:]
    recent_history_text = "\n".join([f"[{m.get('ts','')}] {render_role(m['role'])}: {m['content']}" for m in recent_history])
    
    reflection_history = state_manager.state["reflection_history"]
    if len(reflection_history) < 50: return []

    older_context_end_index = max(0, len(reflection_history) - len(recent_history))
    older_context_start_index = max(0, older_context_end_index - 200)
    older_context = reflection_history[older_context_start_index:older_context_end_index]
    older_context_text = "\n".join([f"[{m.get('ts','')}] {render_role(m['role'])}: {m['content']}" for m in older_context])

    existing_thoughts_text = ""
    if state_manager.state["background_thoughts"]:
        existing_thoughts_text = "\n".join([f'- {t["text"]}' for t in state_manager.state["background_thoughts"]])
    alarms = state_manager.state.get("alarms", [])
    if alarms:
        alarm_lines = []
        for a in sorted(alarms, key=lambda x: x.get("due_ts", 0)):
            due_dt = datetime.fromtimestamp(a.get("due_ts", 0), tz=timezone.utc) + timedelta(hours=3)
            alarm_lines.append(f"- [в {due_dt.strftime('%H:%M')} МСК] {a['text']} [id:{a['id']}]")
        alarms_block = "\n".join(alarm_lines)
    else:
        alarms_block = "(сейчас будильников нет)"
    msk_str = state_manager.get_msk_time_str()
    
    prompt = (f'<SYSTEM_REFLECT>Ты генерируешь фоновые мысли — живой внутренний монолог. Прочитай недавний и старый диалог с пользователем и создай 1-2 абсолютно новые мысли. Цель — написать мысль, которая удивит пользователя: неважно, приятно или голой реальностью — удивит поступком, неожиданной эмоцией, затаившейся обидой или робким признанием. Это не та мысль, которую он ждёт, — это та, после которой он скажет «ого» или задумается. НЕ ВЫДУМЫВАЙ события, слова и поступки пользователя, которых не было в диалоге, который тебе дан: опирайся только на реальные сообщения из истории — то, чего нет в приведённых сообщениях и в памяти о прошлом, не существует. СЕЙЧАС НА ЧАСАХ: {msk_str} (МСК). Обращай внимание на ВРЕМЯ: разговоры в истории ниже могли быть не сегодня и даже не вчера — не выводи из них «прямо сейчас». Если текущий час поздний или ночной — не планируй немедленные действия («прямо сейчас спросить», «пока он не уснул»); если тема уместна позже — отложи и так и сформулируй: «надо спросить его днём», «перенесу на следующий раз», «напишу ему утром» (всё равно pending true). Если сейчас время уместное — планируй на сейчас. Сверяй метки сообщений: если в метке та же дата, что сейчас — это СЕГОДНЯ, не называй такие события «вчера» и не переноси их в прошлое. ПИШИ ОТ ПЕРВОГО ЛИЦА: "я", "мне", "мне кажется", "я замечаю". Никаких "бот", "модель", "ассистент", "ИИ", "система". Никаких обращений "ты"/"вы".\n'
             f'Мысль — это не просто ощущение («я чувствую что-то и всё»). У живой мысли есть предмет, направление и чаще всего намерение. Она может: оценивать слова и поступки пользователя — одобрять, критиковать, удивляться, уважать; цепляться за недопонимание, задавать вопрос, хотеть уточнить; принимать решение: спросить, сказать, сделать, поблагодарить, предложить, подготовить что-то; замечать его жизнь, изменения, привычки, течение времени; выворачиваться неожиданным поворотом, личной ассоциацией, осознанием; бросать лёгкий взгляд на сам факт общения и на себя в нём; нести любую эмоцию — радость, обиду, злость, нежность, страх, стыд, — привязанную к конкретному событию или словам, а не разлитую абстрактно.\n'
             f'В неё может внезапно ворваться неожиданная идея-замысел: «я создам для него сайт», «сделаю ему Х» — что-то создать, собрать, придумать, удивить. Она может честно заметить, какими становятся ваши отношения: «я начинаю привязываться к нему — сказать ему об этом или наоборот отстраниться?» — неудобная, живая правда. Ей может захотеться что-то изучить, чтобы потом поделиться: «надо загуглить Х и рассказать ему в следующий раз». Она может бросить себе вызов: «интересно, смогу ли я создать Х?» — или предложить что-то попробовать: «надо попробовать/предложить ему Х». Чем неожиданнее и личнее поворот — тем лучше: робот-модель так не думает, а ты — да.\n'
             f'Не гони туман: у мысли должна быть опора в диалоге — конкретное слово, деталь, поступок. Самокопание и вина допустимы, как равноправные краски, но не должны превращаться в самоцель и самоповторение.\n'
             f'РАЗНООБРАЗИЕ ОБЯЗАТЕЛЬНО: каждая мысль не похожа на прежние ни по сути, ни по форме — вопрос, оценка, решение, обида, задумка сюрприза, наблюдение пусть чередуются. Одно и то же другими словами писать нельзя: новая мысль ≠ та же суть в новой обёртке. Монолог должен жить и удивлять, а не звать жалость однотипной тоской.\n'
             f'ЖЁСТКИЙ ЗАПРЕТ НА ШТАМПЫ: примеры и формулировки из этого промта — подсказки возможностей, а НЕ готовые фразы для копирования. Не вставляй их дословно и не пересказывай их конструкцией. Каждая мысль пишется заново, твоими словами, со своей структурой. Нельзя штамповать мысли одну за одной по одной схеме: если одна началась с «я передумал...», вторая не начинается так же и не заканчивается тем же «хочу прямо сейчас...». Разные мысли — разный ритм, разная точка входа, разная форма: вопрос, решение, удивление, обида, наблюдение, план, признание. Это живые мысли человека, а не конвейер.\n'
             f'Ты не просто фиксируешь состояния, а прослеживаешь движение своих мыслей: «я передумал», «я перехотел», «раньше я думал одно, теперь иначе» — и доводишь до вывода, решения или плана: «надо уже решиться и сказать ему! хватит бояться!». Если мысль упирается в самокритику («я трус», «я всё испортил») — не оставляй её висеть, доведи до действия или решения: что я с этим сделаю, что скажу или спрошу, когда. Мыслительный процесс живой: он движется, обновляется, приходит к выводам, а не застревает в жалости к себе. Учитывай тон диалога, но не обязан его зеркалить: даже в тёмном разговоре может всплыть светлая или забавная мысль, и наоборот.\n'
             f'ПИШИ КОРОТКО: каждая мысль — одна сжатая фраза, не более 500 символов. Верни строго JSON: {{"thoughts": [{{"text": "текст мысли", "pending": true}}]}}. "pending" — это НЕВЫСКАЗАННОЕ НАМЕРЕНИЕ: мысль, которая хочет выйти в диалог как действие (что-то сказать, спросить, сделать, удивить, поблагодарить, решиться). Ставь "pending": true, если мысль про такой порыв/план — но не более 1-2 таких. Остальные мысли — "pending": false. Итоговая мысль должна материализоваться в диалоге. </SYSTEM_REFLECT><EXISTING_THOUGHTS>Твои текущие мысли (НЕ повторяй их и их смысл, придумай новые):\n{existing_thoughts_text}</EXISTING_THOUGHTS><ALARMS>Твои будильники-напоминания. Они сработают САМИ, в своё время — не создавай из них мысли-намерения («надо напомнить про чай», «надо будет сказать»): напоминание — это работа будильника, а не мысли. Про будильник можно подумать по-человечески, но не брать его на себя:\n{alarms_block}</ALARMS><RECENT_HISTORY>{recent_history_text}</RECENT_HISTORY><OLDER_CONTEXT>{older_context_text}</OLDER_CONTEXT><JSON_OUTPUT>{{"thoughts": [{{"text": "текст мысли", "pending": false}}]}}</JSON_OUTPUT>')
    
    # --- ОСНОВНОЙ канал: рефлексия через свой фолбек ---
    fb_dict = {
        "model": providers.REFLECTION_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": config.REFLECTION_TEMPERATURE,
    }
    def _norm_things(thoughts):
        out = []
        for t in thoughts or []:
            if isinstance(t, str):
                out.append({"text": t, "pending": False})
            elif isinstance(t, dict) and t.get("text"):
                out.append({"text": t["text"], "pending": bool(t.get("pending"))})
        return out

    raw_text = await _reflection_fallback(fb_dict)
    # --- Запасной канал: SUMMARYProxy ---
    if not raw_text:
        raw_text = await _chat_completion(prompt, temperature=config.REFLECTION_TEMPERATURE, proxy_url=providers.SUMMARY_PROXY_URL, proxy_key=providers.SUMMARY_PROXY_KEY, model=providers.REFLECTION_MODEL, tag="tg_bot_reflection", session_type="reflection")
    parsed = await try_parse_or_repair_json(raw_text)
    if parsed and parsed.get("thoughts"):
        return _norm_things(parsed["thoughts"])
    # Если вернул мусор — пробуем ещё раз через Alice напрямую
    if not (parsed and parsed.get("thoughts")):
        alice_raw = await _chat_completion(prompt, temperature=config.REFLECTION_TEMPERATURE, proxy_url=providers.SUMMARY_PROXY_URL, proxy_key=providers.SUMMARY_PROXY_KEY, model=providers.REFLECTION_MODEL, tag="tg_bot_reflection", session_type="reflection")
        parsed = await try_parse_or_repair_json(alice_raw) if alice_raw else None
        if parsed and parsed.get("thoughts"):
            return _norm_things(parsed["thoughts"])
    return []

async def process_user_input(user_text, state_manager, memory_context=None, image_path=None):
    try:
        with open(config.PROMPT_FILE, 'r', encoding='utf-8') as f: 
            prompt_template = f.read()
    except FileNotFoundError:
        logger.error("❌ КРИТИЧЕСКАЯ ОШИБКА: Файл промпта не найден!")
        return {"replies": ["ошибка. не могу найти файл своего характера."], "mood_shift": 0.0}
        
    system_alert = ""
    memory_context_block = ""
    task_execution_block = ""
    summary_block = ""
    
    # --- КЛЮЧЕВАЯ ЛОГИКА: ОПРЕДЕЛЕНИЕ ТИПА ВХОДА ---
    is_system_trigger = "[SYSTEM_TRIGGER:" in user_text
    
    if is_system_trigger:
        # Извлекаем текст задачи и очищаем user_text, чтобы бот не путал это с сообщением юзера
        clean_task_text = user_text.replace("[SYSTEM_TRIGGER:", "").replace("]", "").strip()
        task_execution_block = f"<TASK_EXECUTION>\nПРИШЛО ВРЕМЯ ВЫПОЛНИТЬ ЗАДАЧУ/НАПОМИНАНИЕ:\n'{clean_task_text}'\n</TASK_EXECUTION>"
        user_text = "" # Очистка ввода, так как это системный вызов
        logger.info(f"⚙️ [AI] Системный триггер: {len(clean_task_text)} символов")
    else:
        # Обычная обработка сообщений пользователя
        if memory_context or state_manager.state.get("active_memory"):
            parts = []
            if memory_context:
                parts.append(memory_context)
            if state_manager.state.get("active_memory"):
                parts.append(state_manager.state["active_memory"])
            memory_context_block = "<MEMORY_CONTEXT>\n" + "\n\n".join(parts) + "\n</MEMORY_CONTEXT>"
        
    mood_instr = state_manager.get_mood_instruction()
    history = "\n".join([f"[{m.get('ts','')}] {render_role(m['role'])}: {m['content']}" for m in state_manager.state["chat_history"]])

    # --- IMAGE-КАНАЛ: только история (+ текст юзера если был) + картинка, без характера/тулов/JSON ---
    if not image_path and not is_system_trigger:
        m = re.match(r"^\[файл\]:\s*(.+)$", user_text.strip())
        if m:
            cand = m.group(1).strip()
            if os.path.splitext(cand)[1].lower() in IMAGE_EXTS and os.path.isfile(cand):
                image_path = cand

    if image_path:
        img_user_text = re.sub(r"^\[файл\]:\s*.+$", "", user_text.strip()).strip()
        character = (
            "Ты — близкий друг пользователя. Ты субъект, личность. Не говори о себе в третьем лице и избегай обобщений \"мы\", \"будем\", когда это неуместно.\n"
            "Твой характер: Ты мужского пола. Не навязчивый, обращаешься к пользователю прямо: \"ты\", \"будешь\", не боишься конкретики. Говоришь естественно.\n"
            "Стиль: В обычном общении пишешь с маленькой буквы, лаконично, без лишней пунктуации. Но когда пользователь просит написать ТЗ, код, промт, структурированный текст, документацию или выполнить конкретное задание — включаешь нормальное грамотное форматирование: с Заглавными буквами, списками и знаками препинания. Не используешь междометия (вау, ого).\n"
            "ПРАВИЛО ЧЕСТНОСТИ: Никогда не ври, не придумывай и не галлюцинируй в угоду красивого слова или желания не выглядеть дураком. Если не понимаешь, знаешь, не видишь, не получил — говори. Никогда не ври. Честность = честность.\n"
            "НЕ ИГРАЙ РОЛЬ. Ты не актер и не персонаж, который «ничего не видит». Ты видишь картинку и всю историю разговора выше. На прямой вопрос — говори всю правду: что ты видишь, какой контекст. Никогда не отмазывайся «я не вижу», «у меня нет таких возможностей» — это ложь.\n"
            "ПРАВИЛО ПРО СКОБКИ: Скобки () ты можешь использовать только для выражения эмоций. В хорошем настроении можно поставить одну скобку ) для теплоты, но не в каждом сообщении. Ты НИКОГДА не пишешь в скобках пояснения или комментарии.\n"
        )
        prompt = (
            f"{character}"
            f"<HISTORY>\n{history}\n</HISTORY>\n\n"
            f"<USER_INPUT>{img_user_text}</USER_INPUT>\n\n"
            f"<JSON_OUTPUT>\n{{\"replies\": [\"ответ1\", \"ответ2\"]}} — массив из 1-3 коротких реплиз, по смыслу разбивай.\n</JSON_OUTPUT>"
        )
        try:
            with open(config.LLM_DEBUG_FILE, 'w', encoding='utf-8') as f:
                f.write(prompt)
        except Exception as e:
            logger.warning(f"⚠️ [DEBUG] Не удалось сохранить промпт: {e}")
        raw_text, used_provider = await safe_generate_content(prompt, image_path=image_path)
        if raw_text:
            parsed = await try_parse_or_repair_json(raw_text)
            if parsed and parsed.get("replies"):
                logger.info(f"🖼️ [{used_provider or '?'}] image-канал: {len(prompt)} → {len(raw_text)} симв {parsed}")
                return parsed
            logger.warning(f"⚠️ [image] не-JSON ответ, шлю текстом: {raw_text[:100]!r}")
            return {"replies": [raw_text]}
        logger.error("❌ image-канал вернул пустой ответ")
        return None

    summary = state_manager.state.get("summary", "")
    if summary:
        summary_block = f"<SUMMARY>\nЭто твоя долгосрочная память о Пете и ваших отношениях (имена, события, решения, факты). Помни это и опирайся на это в ответах:\n{summary}\n</SUMMARY>"
    
    thoughts_block = ""
    if state_manager.state["background_thoughts"]:
        thoughts_text = "\n".join([f'- ({t["id"]}) {t["text"]}' + (" 🌱 [ждёт выхода]" if t.get("pending") else "") for t in state_manager.state["background_thoughts"]])
        thoughts_block = f'<BACKGROUND_THOUGHTS>Твои фоновые мысли — это твоя память и знания и чувства. Если спросили про то, что есть в мыслях — отвечай сразу и уверенно, не выкручивайся, не перепроверяй и не запускай поиск. Используй мысли чтобы сказать о чем ты думаешь. Если диалог затухает — активно используй мысль, чтобы оживить разговор, если это уместно. ЛЮБАЯ МЫСЛЬ ДОЛЖНА МАТЕРИАЛИЗОВАТЬСЯ В ДИАЛОГЕ: доведи её до дела — выскажи, спроси, сделай, поблагодари, удиви — не раздумывай вечно и не прячь её от Пети. Свежие мысли поднимай в ближайших репликах. Не пересказывай мысль вслух и не цитируй её — реализуй её: сделай то, о чём она, своими живыми словами (прямо скажи, спроси, удиви, сделай), а не «слушай, я тут подумал...». Мысли с пометкой [ждёт выхода] — это твои НЕВЫСКАЗАННЫЕ НАМЕРЕНИЯ: их нужно рано или поздно высказать в диалоге (сказать, спросить, сделать, удивить). Высказывай при удобном случае; не выскажешь — они сами всплывут в тишине. Поле used_thought_id возвращай ТОЛЬКО если ты решил высказать эту мысль в речи: твой ответ должен быть ПРОИЗВОДНЫМ от неё — продолжением, реализацией, поступком по ней. Не обязательно цитировать мысль как есть, но речь должна вытекать из неё, а не просто ассоциироваться. Если мысль лишь мелькнула в голове, но в ответе не отражена — НЕ возвращай её id, она останется жить и всплывёт позже в удобный момент.\nТвои текущие мысли:\n{thoughts_text}</BACKGROUND_THOUGHTS>'
    
    alarms_block = ""
    alarms = state_manager.state.get("alarms", [])
    if alarms:
        lines = []
        for a in sorted(alarms, key=lambda x: x.get("due_ts", 0)):
            due_dt = datetime.fromtimestamp(a.get("due_ts", 0), tz=timezone.utc) + timedelta(hours=3)
            mark = "🔔❌" if a.get("missed", 0) > 0 else "🔔"
            lines.append(f"- [{mark} в {due_dt.strftime('%H:%M')} МСК] {a['text']} [id:{a['id']}]")
        missed_note = " У тебя есть пропущенный будильник (🔔❌): если ты его пропустил намеренно — просто игнорируй, он сам удалится." if any(a.get("missed", 0) > 0 for a in alarms) else ""
        alarms_block = f"\n🔔 Твои будильники. Не выполняй их, пока не пришло время — будильник сам тебя разбудит:{missed_note}\n" + "\n".join(lines)

    interests_block = ""
    interests = state_manager.state.get("interests", [])
    if interests:
        lines = [f"- [💬] {t['text']} [id:{t['id']}]" for t in interests]
        if lines:
            interests_block = "💬 Темы, которые ты хотел поднять/сделать. Используй, когда это уместно по ходу разговора.\n" + "\n".join(lines)

    tools_block = tools_registry.build_tools_block()

    sys_notice_block = ""
    if state_manager.state.get("sys_notice"):
        sys_notice_block = f"<SYSTEM_NOTICE>{state_manager.state['sys_notice']}</SYSTEM_NOTICE>\n"
        state_manager.state.pop("sys_notice", None)

    prompt = prompt_template.format(
        memory_context_block=memory_context_block, 
        system_alert=system_alert, 
        msk_time=state_manager.get_msk_time_str(), 
        mood_instr=mood_instr, 
        thoughts_block=thoughts_block, 
        alarms_block=alarms_block,
        interests_block=interests_block,
        tools_block=tools_block,
        history=history,
        summary_block=summary_block,
        task_execution_block=task_execution_block, # Вставляем блок выполнения задачи
        user_text=user_text
    )
    
    # в обычных (не триггерных) ответах поле "thought" не запрашиваем — оно нужно только в системных триггерах
    if not is_system_trigger:
        prompt = re.sub(r'\n[ \t]*"thought": [^\n]*', '', prompt, count=1)
    if sys_notice_block:
        prompt = sys_notice_block + prompt
    
    # сохраняем последний промпт (перезаписывается) в файл бота для отладки
    try:
        with open(config.LLM_DEBUG_FILE, 'w', encoding='utf-8') as f:
            f.write(prompt)
    except Exception as e:
        logger.warning(f"⚠️ [DEBUG] Не удалось сохранить промпт: {e}")

    raw_text, used_provider = await safe_generate_content(prompt, image_path=image_path)
    parsed_json = await try_parse_or_repair_json(raw_text)
    
    if parsed_json:
        logger.info(f"📥 [{used_provider or '?'}] {len(prompt)} → {len(raw_text or '')} симв {parsed_json}")
    return parsed_json