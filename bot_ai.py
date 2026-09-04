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
    match = re.search(r'\{.*\}', text, re.DOTALL)
    if not match:
        return text.strip()
    result = match.group(0).strip()
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
                    return json.dumps(parsed, ensure_ascii=False)
                except json.JSONDecodeError:
                    pass
        return result
    except json.JSONDecodeError:
        return result

def render_role(role):
    if role == "tool":
        return "tool"
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
                with open("/root/tg_bot/last_raw_response.txt", "w") as f:
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


async def safe_generate_content(prompt, temperature=0.85, image_path=None):
    """Генерация с фолбеком: основной провайдер → MAIN_FALLBACK.
    Возвращает (text, provider_name) — имя реально сработавшего провайдера,
    чтобы лог не врал про источник ответа."""
    if is_stopped():
        return None, None
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
        "Ты — модуль выжимки вывода shell-команды для чат-бота. "
        "Тебе дают: команду, ЗАЧЕМ она выполнялась (описание цели), "
        "хвост диалога (контекст) и полный вывод команды. "
        "Сделай ОЧЕНЬ короткую выжимку вывода, максимально полезную именно для заявленной цели. "
        "Не упускай ничего, что относится к цели даже косвенно (например искомый токен или параметр). "
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

async def try_parse_or_repair_json(raw_text):
    if not raw_text:
        return None

    # Логируем сырой ответ в файл
    try:
        with open("/root/tg_bot/last_raw_response.txt", "w") as f:
            f.write(f"=== RAW RESPONSE ({len(raw_text)} симв) ===\n")
            f.write(raw_text[:5000])
            f.write(f"\n=== END ===\n")
    except Exception:
        pass

    try:
        return json.loads(clean_json_response(raw_text))
    except (json.JSONDecodeError, AttributeError, ValueError):
        logger.warning(f"⚠️ [JSON] Ошибка парсинга. Отправляю как текст.")
        # Если модель вернула просто текст без JSON — оборачиваем и отправляем
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
        "- ПИШИ  СЖАТО И ПЛОТНО, НЕ ТЕРЯЯ СМЫСЛ. ЖЁСТКИЙ ЛИМИТ: максимум 800 символов.\n"
        "- Без воды, оценок, эпитетов, вводных фраз. Только суть: факты, решения, имена, договорённости.\n"
        "- Если часть старого саммари устарела/опровергнута — замени её, не дублируй.\n"
        "- Приоритет: что важно помнить ДОЛГО.\n"
        "- ПИШИ ОТ ПЕРВОГО ЛИЦА: 'я', 'мне', 'мне показалось', 'я решил', 'я предложил'. Пример: 'Петя сказал X, я Y', 'Мы решили Z', 'Петя обиделся на меня за X', 'Я слелал X, но у меня не получилось'. Никаких упоминаний 'бот', 'модель', 'ассистент', 'ИИ', 'система'. Петя - пользователь. Ты - ео собеседник.\n"
        "- НЕ включай в саммари данные о будильниках, напоминаниях, таймерах, алармах — они динамические, меняются и только мешают.\n\n"
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
    older_context_text = "\n".join([f"{render_role(m['role'])}: {m['content']}" for m in older_context])

    existing_thoughts_text = ""
    if state_manager.state["background_thoughts"]:
        existing_thoughts_text = "\n".join([f'- {t["text"]}' for t in state_manager.state["background_thoughts"]])
    
    prompt = f'<SYSTEM_REFLECT>Ты генерируешь фоновые мысли — внутренний монолог. Прочитай недавний и старый диалог с ползователем и создай 1-2 новые мысли. ПИШИ ОТ ПЕРВОГО ЛИЦА: "я", "мне", "мне кажется", "я замечаю", "я удивляюсь". Никаких упоминаний "бот", "модель", "ассистент", "ИИ", "аналитик", "система". Никаких обращений "ты"/"вы". Бзудь внимателем к деталям. Мысли не должны быть отражением очевидного из разговора. Замечай то, чего не говорили в диалоге. Замечай то, что ты подозреваешь, искренне желаешь, надеешься. Выражай разный спектр эмоэций в контексте разговора. О чем-то радуешься, беспокоишься, привыкаешь, злишся, обижаешься. Пример (не шаблон): "Мне было обидно/приятно/больно/радостно когда Петя сказал/сделал/показал X", "Инстересно...", Важно: не используй пример как шаблон, анализируй контекст внимательно, мысли могут быть всем чем угодно: любопытством, заботой, страхом, нежностью, подозрением, злостью или чем-то совершенно другим или смешанным. Описывай мысли точно, используй прилагательные и слова которые точно описывают мысли. КРИТИЧЕСКИ ВАЖНО: не повторяй уже существующие мысли и не создавай похожих по смыслу — каждая новая мысль должна быть уникальной, о чём-то ещё не исследованном. Важно: Не говори факты и рассуждения, которые не являются мыслями. Мысли - это внутренние переживания определенных эмоций, а не рассуждения о проделанной работе. Верни строго JSON: {{"thoughts": ["текст мысли"]}}.</SYSTEM_REFLECT><EXISTING_THOUGHTS>Твои текущие мысли (НЕ повторяй их и их смысл, придумай новые):\n{existing_thoughts_text}</EXISTING_THOUGHTS><RECENT_HISTORY>{recent_history_text}</RECENT_HISTORY><OLDER_CONTEXT>{older_context_text}</OLDER_CONTEXT><JSON_OUTPUT>{{"thoughts": ["текст мысли"]}}</JSON_OUTPUT>'
    
    # --- ОСНОВНОЙ канал: рефлексия через свой фолбек ---
    fb_dict = {
        "model": providers.REFLECTION_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": config.REFLECTION_TEMPERATURE,
    }
    raw_text = await _reflection_fallback(fb_dict)
    # --- Запасной канал: SUMMARYProxy ---
    if not raw_text:
        raw_text = await _chat_completion(prompt, temperature=config.REFLECTION_TEMPERATURE, proxy_url=providers.SUMMARY_PROXY_URL, proxy_key=providers.SUMMARY_PROXY_KEY, model=providers.REFLECTION_MODEL, tag="tg_bot_reflection", session_type="reflection")
    parsed = await try_parse_or_repair_json(raw_text)
    if parsed and parsed.get("thoughts"):
        return parsed["thoughts"]
    # Если вернул мусор — пробуем ещё раз через Alice напрямую
    if not (parsed and parsed.get("thoughts")):
        alice_raw = await _chat_completion(prompt, temperature=config.REFLECTION_TEMPERATURE, proxy_url=providers.SUMMARY_PROXY_URL, proxy_key=providers.SUMMARY_PROXY_KEY, model=providers.REFLECTION_MODEL, tag="tg_bot_reflection", session_type="reflection")
        parsed = await try_parse_or_repair_json(alice_raw) if alice_raw else None
        if parsed and parsed.get("thoughts"):
            return parsed["thoughts"]
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
        logger.info(f"⚙️ [AI] Режим выполнения задачи: {clean_task_text}")
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

    summary = state_manager.state.get("summary", "")
    if summary:
        summary_block = f"<SUMMARY>\nЭто твоя долгосрочная память о Пете и ваших отношениях (имена, события, решения, факты). Помни это и опирайся на это в ответах:\n{summary}\n</SUMMARY>"
    
    thoughts_block = ""
    if state_manager.state["background_thoughts"]:
        thoughts_text = "\n".join([f'- ({t["id"]}) {t["text"]}' for t in state_manager.state["background_thoughts"]])
        thoughts_block = f'<BACKGROUND_THOUGHTS>Твои фоновые мысли — это твоя память и знания и чувства. Если спросили про то, что есть в мыслях — отвечай сразу и уверенно, не выкручивайся, не перепроверяй и не запускай поиск. Используй мысли чтобы сказать о чем ты думаешь. Если диалог затухает — активно используй мысль, чтобы оживить разговор, если это уместно.\nТвои текущие мысли:\n{thoughts_text}</BACKGROUND_THOUGHTS>'
    
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

    prompt = prompt_template.format(
        memory_context_block=memory_context_block, 
        system_alert=system_alert, 
        msk_time=state_manager.get_msk_time_obj().strftime("%H:%M"), 
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
    
    # сохраняем последний промпт (перезаписывается) в файл бота для отладки
    try:
        with open(config.LLM_DEBUG_FILE, 'w', encoding='utf-8') as f:
            f.write(prompt)
    except Exception as e:
        logger.warning(f"⚠️ [DEBUG] Не удалось сохранить промпт: {e}")

    # Определяем, является ли вход картинкой: "[файл]: <путь>" с расширением-картинкой.
    # image_path может прийти извне (file.read на картинке из bot_handlers) или быть
    # найденным здесь — при реальной отправке фото в чат ("[файл]: <путь>" в user_text).
    if not image_path and not is_system_trigger:
        m = re.match(r"^\[файл\]:\s*(.+)$", user_text.strip())
        if m:
            cand = m.group(1).strip()
            if os.path.splitext(cand)[1].lower() in IMAGE_EXTS and os.path.isfile(cand):
                image_path = cand

    raw_text, used_provider = await safe_generate_content(prompt, image_path=image_path)
    parsed_json = await try_parse_or_repair_json(raw_text)
    
    if parsed_json:
        logger.info(f"📥 [{used_provider or '?'}] {len(prompt)} → {len(raw_text or '')} симв {parsed_json}")
    return parsed_json