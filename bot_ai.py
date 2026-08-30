# --- START OF FILE bot_ai.py ---

import logging
import asyncio
import json
import re
import urllib.request
from datetime import datetime, timedelta, timezone

import config
from rag import insert_to_rag, query_rag
import tools_registry

logger = logging.getLogger(__name__)

def clean_json_response(text):
    match = re.search(r'\{.*\}', text, re.DOTALL)
    return match.group(0).strip() if match else text.strip()

def render_role(role):
    if role == "tool":
        return "tool"
    return "user" if role == "user" else "assistant"

async def safe_generate_content(prompt, temperature=0.85):
    payload = json.dumps({
        "model": config.GEMINI_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": temperature
    }).encode('utf-8')

    req = urllib.request.Request(
        f"{config.GEMINI_PROXY_URL}/v1/chat/completions",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {config.GEMINI_PROXY_KEY}"
        }
    )

    for attempt in range(3):
        try:
            loop = asyncio.get_running_loop()
            resp = await loop.run_in_executor(None, lambda: urllib.request.urlopen(req, timeout=180))
            data = json.loads(resp.read().decode('utf-8'))
            text = data["choices"][0]["message"]["content"]
            return text
        except Exception as e:
            logger.error(f"❌ [GEMINI] Ошибка (попытка {attempt+1}): {e}")
            if attempt < 2:
                await asyncio.sleep(2)
    return None

async def safe_generate_deepseek(prompt, temperature=0.7, proxy_url=None, proxy_key=None, model=None, tag="tg_bot_deepseek"):
    """Генерация через DeepSeek-прокси (OpenAI-совместимый). Модель/прокси берутся
    из конфига; для рефлексии можно указать свои. Возвращает текст или None."""
    model = model or config.DEEPSEEK_MODEL
    proxy_url = proxy_url or config.DEEPSEEK_PROXY_URL
    proxy_key = proxy_key or config.DEEPSEEK_PROXY_KEY
    logger.info(f"📤 [DEEPSEEK:{model}] Отправка промпта длиной: {len(prompt)} символов")
    payload = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": temperature
    }).encode('utf-8')

    req = urllib.request.Request(
        f"{proxy_url}/v1/chat/completions",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {proxy_key}"
        }
    )

    for attempt in range(3):
        try:
            loop = asyncio.get_running_loop()
            resp = await loop.run_in_executor(None, lambda: urllib.request.urlopen(req, timeout=180))
            data = json.loads(resp.read().decode('utf-8'))
            text = data["choices"][0]["message"]["content"]
            logger.info(f"📤 [DEEPSEEK:{model}] Ответ получен, длина: {len(text)} символов")
            return text
        except Exception as e:
            logger.error(f"❌ [DEEPSEEK:{model}] Ошибка (попытка {attempt+1}): {e}")
            if attempt < 2:
                await asyncio.sleep(2)
    return None

async def _summary_fallback(payload_dict, tag="SUMMARY"):
    """Фолбек на анонимный ChatGPT-скрапер, если основной SUMMARY-провайдер упал."""
    try:
        req = urllib.request.Request(
            f"{config.CHATGPT_FALLBACK_URL}/v1/chat/completions",
            data=json.dumps(payload_dict).encode('utf-8'),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {config.CHATGPT_FALLBACK_KEY}"
            }
        )
        loop = asyncio.get_running_loop()
        resp = await loop.run_in_executor(None, lambda: urllib.request.urlopen(req, timeout=120))
        data = json.loads(resp.read().decode('utf-8'))
        text = data["choices"][0]["message"]["content"].strip()
        logger.info(f"🆘 [{tag}] Фолбек на ChatGPT сработал: {len(text)} символов")
        return text
    except Exception as e:
        logger.error(f"❌ [{tag}] Фолбек на ChatGPT не сработал: {e}")
        return None

async def summarize_tool_output(cmd, purpose, output, state_manager):
    """Выжимка большого вывода команды через DeepSeek (≤ TOOL_RESULT_LIMIT).
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
        "model": config.SUMMARY_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ],
        "temperature": 0.3,
        "user": "tg_bot_tool_summary"
    }

    req = urllib.request.Request(
        f"{config.SUMMARY_PROXY_URL}/v1/chat/completions",
        data=json.dumps(payload_dict).encode('utf-8'),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {config.SUMMARY_PROXY_KEY}"
        }
    )

    for attempt in range(2):
        try:
            loop = asyncio.get_running_loop()
            resp = await loop.run_in_executor(None, lambda: urllib.request.urlopen(req, timeout=60))
            data = json.loads(resp.read().decode('utf-8'))
            text = data["choices"][0]["message"]["content"].strip()
            logger.info(f"✂️ [SUMMARY] Выжимка вывода: {len(text)} символов")
            return text[:config.TOOL_RESULT_LIMIT]
        except Exception as e:
            logger.error(f"❌ [SUMMARY] Ошибка выжимки (попытка {attempt+1}): {e}")
            if attempt < 1:
                await asyncio.sleep(2)
    # Фолбек на ChatGPT (Алиса упала)
    fb = await _summary_fallback(payload_dict, "SUMMARY")
    if fb:
        return fb[:config.TOOL_RESULT_LIMIT]
    # Финальный фолбек при полном отказе — режем жадно
    return output[:config.TOOL_RESULT_LIMIT] + f"\n... (всего {len(output)} симв.)"

async def summarize_search_result(search_query, output, state_manager):
    """Выжимка большого поискового результата через DeepSeek (≤ TOOL_RESULT_LIMIT).
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
        "model": config.SUMMARY_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ],
        "temperature": 0.3,
        "user": "tg_bot_search_summary"
    }

    req = urllib.request.Request(
        f"{config.SUMMARY_PROXY_URL}/v1/chat/completions",
        data=json.dumps(payload_dict).encode('utf-8'),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {config.SUMMARY_PROXY_KEY}"
        }
    )

    for attempt in range(2):
        try:
            loop = asyncio.get_running_loop()
            resp = await loop.run_in_executor(None, lambda: urllib.request.urlopen(req, timeout=60))
            data = json.loads(resp.read().decode('utf-8'))
            text = data["choices"][0]["message"]["content"].strip()
            logger.info(f"✂️ [SUMMARY] Выжимка поиска: {len(text)} символов")
            return text[:config.TOOL_RESULT_LIMIT]
        except Exception as e:
            logger.error(f"❌ [SUMMARY] Ошибка выжимки поиска (попытка {attempt+1}): {e}")
            if attempt < 1:
                await asyncio.sleep(2)
    # Фолбек на ChatGPT (Алиса упала)
    fb = await _summary_fallback(payload_dict, "SUMMARY-SEARCH")
    if fb:
        return fb[:config.TOOL_RESULT_LIMIT]
    # Финальный фолбек при полном отказе — режем жадно
    return output[:config.TOOL_RESULT_LIMIT] + f"\n... (всего {len(output)} симв.)"

def perplexity_search(query):
    if not config.PERPLEXITY_COOKIE or not config.PERPLEXITY_RW_TOKEN:
        return None
    import uuid as _uuid
    params = {
        "last_backend_uuid": str(_uuid.uuid4()),
        "frontend_uuid": str(_uuid.uuid4()),
        "read_write_token": config.PERPLEXITY_RW_TOKEN,
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
            "Cookie": config.PERPLEXITY_COOKIE,
        },
    )
    try:
        resp = urllib.request.urlopen(req, timeout=90)
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

async def search_web(query):
    logger.info(f"🔍 [SEARCH] Поиск: {query}")
    payload = json.dumps({
        "model": config.DEEPSEEK_MODEL,
        "messages": [
            {"role": "user", "content": query}
        ],
        "temperature": 0.3,
        "search": True,
        "user": "tg_bot_search"
    }).encode('utf-8')

    req = urllib.request.Request(
        f"{config.DEEPSEEK_PROXY_URL}/v1/chat/completions",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {config.DEEPSEEK_PROXY_KEY}"
        }
    )

    for attempt in range(2):
        try:
            loop = asyncio.get_running_loop()
            resp = await loop.run_in_executor(None, lambda: urllib.request.urlopen(req, timeout=60))
            data = json.loads(resp.read().decode('utf-8'))
            text = data["choices"][0]["message"]["content"]
            logger.info(f"🔍 [SEARCH] Результат: {len(text)} символов")
            return text
        except Exception as e:
            logger.error(f"❌ [SEARCH] Ошибка (попытка {attempt+1}): {e}")
            if attempt < 1:
                await asyncio.sleep(2)
    try:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, lambda: perplexity_search(query))
    except Exception as e:
        logger.error(f"❌ [PERPLEXITY] фолбек-ошибка: {e}")
    return None

_transcribe_key_idx = 0

async def transcribe_voice(audio_bytes, filename):
    global _transcribe_key_idx
    if not config.GROQ_KEYS:
        logger.error("❌ [TRANSCRIBE] Нет Groq ключей")
        return None
    
    key = config.GROQ_KEYS[_transcribe_key_idx % len(config.GROQ_KEYS)]
    _transcribe_key_idx += 1
    
    import io
    import requests as req
    try:
        loop = asyncio.get_running_loop()
        def do_request():
            return req.post(
                "https://api.groq.com/openai/v1/audio/transcriptions",
                headers={"Authorization": f"Bearer {key}"},
                data={"model": "whisper-large-v3-turbo", "language": "ru"},
                files={"file": (filename, io.BytesIO(audio_bytes), "audio/ogg")},
                timeout=30,
                proxies={"https": "http://15Uo6V:3HF2Fh@170.83.236.245:8000", "http": "http://15Uo6V:3HF2Fh@170.83.236.245:8000"},
            )
        resp = await loop.run_in_executor(None, do_request)
        logger.info(f"🎤 [TRANSCRIBE] Groq ответ: {resp.status_code} -> {resp.text[:300]}")
        data = resp.json()
        text = data.get('text', '')
        logger.info(f"🎤 [TRANSCRIBE] Распознано: {text[:100]}")
        return text
    except Exception as e:
        logger.error(f"❌ [TRANSCRIBE] Ошибка: {e}")
        return None

async def try_parse_or_repair_json(raw_text):
    if not raw_text:
        return None

    try:
        return json.loads(clean_json_response(raw_text))
    except (json.JSONDecodeError, AttributeError, ValueError):
        logger.warning(f"⚠️ [JSON] Ошибка парсинга. Запускаю Аварийное Восстановление JSON.")
        repair_prompt = f"Ответ AI содержит ошибку в JSON. Исправь его. ВАЖНО: Ответ должен быть в поле 'replies': ['текст']. Не используй поле 'text' для ответа. Вот нерабочий ответ:\n\n{raw_text}"
        repaired = await safe_generate_content(repair_prompt, temperature=0.0)
        if repaired:
            try:
                repaired_json = json.loads(clean_json_response(repaired))
                logger.info("✅ [JSON] Аварийное Восстановление JSON успешно!")
                return repaired_json
            except Exception:
                logger.error(f"❌ [JSON] Аварийное Восстановление НЕ удалось.")
    return None

async def retrieve_memory(user_query, query_topic, state_manager):
    logger.info(f"🧠 [MEMORY] Запущен процесс воспоминания по теме: '{query_topic}'")
    
    r1 = r2 = ""
    if query_topic:
        r1, r2 = await asyncio.gather(
            query_rag(user_query, top_k=5),
            query_rag(query_topic, top_k=5),
        )
        r1 = r1 or ""
        r2 = r2 or ""
    result = "\n---\n".join(x for x in (r1, r2) if x)
    logger.info(f"🧠 [MEMORY] Поиск по '{query_topic}': r1={len(r1)} симв, r2={len(r2)} симв, всего={len(result)} симв")
    if result:
        logger.info(f"🧠 [MEMORY] Первые 100 символов результата: {result[:100]!r}")

    if result:
        # Запись в историю: короткий результат — целиком, длинный — только факт "вспоминал"
        if len(result) <= config.TOOL_RESULT_LIMIT:
            rec = f'🧠 вспоминал: "{query_topic}" →\n{result}'
        else:
            rec = f'🧠 вспоминал: "{query_topic}" (результат большой, без текста)'
        await state_manager.add_tool_record(rec)

        memory_context = f"ВНИМАНИЕ! Это приоритетная задача. Пользователь просит тебя что-то вспомнить. Вот контекст из памяти:\n---\n{result}\n---\nТвоя задача — изучить контекст и ответить на вопрос: '{user_query}'. Если ты уже знаешь ответ из контекста диалога — используй его. Следуй своему характеру. Если ответа нет, честно признайся."
        if len(result) > config.TOOL_RESULT_LIMIT:
            memory_context += "\n\nПРИМЕЧАНИЕ: результат воспоминания оказался длинным — отвечай пользователю КОРОТКО, по существу."
    else:
        memory_context = f"Пользователь спрашивает: '{user_query}'. Память пуста или произошла ошибка. Если ответа нет, честно признайся."
    
    return await process_user_input(user_query, state_manager, memory_context=memory_context)

async def update_longterm_summary(state_manager):
    """Обновляет долгосрочное саммари через DeepSeek каждые 50 сообщений."""
    logger.info(f"📌 [SUMMARY] Запуск обновления саммари (messages_since_summary={state_manager.state.get('messages_since_summary', 0)})")

    # Последние 50 сообщений диалога
    recent = state_manager.state["chat_history"][-50:]
    if not recent:
        logger.warning("📌 [SUMMARY] Нет сообщений для саммари")
        return

    recent_text = "\n".join([f"[{m.get('ts','')}] {render_role(m['role'])}: {m['content']}" for m in recent])
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
        "ОТВЕЧАЙ ТОЛЬКО JSON (без объяснений): {\"summary\": \"саммари до 800 символов\", \"done_interests\": [\"id1\", \"id2\"]}. "
        "done_interests — список id выполненных интересов (может быть пустым)."
    )

    user_prompt = (
        f"ПРОШЛОЕ САММАРИ:\n{prev_summary if prev_summary else '(пусто)'}\n\n"
        f"---\n\nНОВЫЕ СООБЩЕНИЯ:\n{recent_text}\n\n"
        f"---\n\nИНТЕРЕСЫ (темы, которые ты хотел поднять):\n{interests_text}"
    )

    payload_dict = {
        "model": config.SUMMARY_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ],
        "temperature": 0.3,
        "user": "tg_bot_summary"
    }

    req = urllib.request.Request(
        f"{config.SUMMARY_PROXY_URL}/v1/chat/completions",
        data=json.dumps(payload_dict).encode('utf-8'),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {config.SUMMARY_PROXY_KEY}"
        }
    )

    for attempt in range(2):
        try:
            loop = asyncio.get_running_loop()
            resp = await loop.run_in_executor(None, lambda: urllib.request.urlopen(req, timeout=60))
            data = json.loads(resp.read().decode('utf-8'))
            text = data["choices"][0]["message"]["content"].strip()
            logger.info(f"📌 [SUMMARY] Ответ получен: {len(text)} символов")
            try:
                parsed = json.loads(clean_json_response(text))
                summary_text = (parsed.get("summary") or text).strip()
                legit_ids = {t["id"] for t in interests}
                done = [x for x in (parsed.get("done_interests") or []) if x in legit_ids]
                await state_manager.set_summary(summary_text)
                if done:
                    await state_manager.remove_interests(done)
                return summary_text
            except (json.JSONDecodeError, AttributeError):
                logger.warning("⚠️ [SUMMARY] Ответ не JSON — сохраняю как есть")
                await state_manager.set_summary(text)
                return text
        except Exception as e:
            logger.error(f"❌ [SUMMARY] Ошибка (попытка {attempt+1}): {e}")
            if attempt < 1:
                await asyncio.sleep(2)
    # Фолбек на ChatGPT (Алиса упала) — сохраняем сырой текст, JSON не ждём
    fb = await _summary_fallback(payload_dict, "SUMMARY-LONG")
    if fb:
        await state_manager.set_summary(fb)
        return fb
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
    
    prompt = f'<SYSTEM_REFLECT>Ты генерируешь фоновые мысли — свой внутренний монолог. Прочитай свой недавний и старый диалог с ползователем и создай 1-2 новые мысли. ПИШИ ОТ ПЕРВОГО ЛИЦА: "я", "мне", "мне кажется", "я замечаю", "я удивляюсь". Никаких упоминаний "бот", "модель", "ассистент", "ИИ", "аналитик", "система". Никаких обращений "ты"/"вы". Бзудь внимателем к деталям и глубоким. Замечай то, чего не говорили, но чувствовали. Замечай то, что ты подозреваешь, искренне желаешь, надеешься. Выражай разный спектр эмоэций в контексте разговлра. О чем то радуешься, беспокоишься, привыкаешь, злишся, обижаешься? Пример(не шаблон): "Мне было обидно/приятно/больно/радостно когда Петя сказал/сделал/показал X", "Инстересно...", Важно: не используй пример как шаблон, анализируй контекст внимательно, твои мысли могут быть всем чем угодно: любопытством, заботой, страхом, нежностью, подозрением, злосью или чем-то совершенно другим или смешанным. Описывай мысли максимально чуственно, используй прилагательные и слова которые точно описывают твои мысли. КРИТИЧЕСКИ ВАЖНО: не повторяй уже существующие мысли и не создавай похожих по смыслу — каждая новая мысль должна быть уникальной, о чём-то ещё не исследованном. Верни строго JSON: {{"thoughts": ["текст мысли"]}}.</SYSTEM_REFLECT><EXISTING_THOUGHTS>Твои текущие мысли (НЕ повторяй их и их смысл, придумай новые):\n{existing_thoughts_text}</EXISTING_THOUGHTS><RECENT_HISTORY>{recent_history_text}</RECENT_HISTORY><OLDER_CONTEXT>{older_context_text}</OLDER_CONTEXT><JSON_OUTPUT>{{"thoughts": ["текст мысли"]}}</JSON_OUTPUT>'
    
    raw_text = await safe_generate_deepseek(prompt, temperature=config.REFLECTION_TEMPERATURE, proxy_url=config.SUMMARY_PROXY_URL, proxy_key=config.SUMMARY_PROXY_KEY, model=config.REFLECTION_MODEL, tag="tg_bot_reflection")
    if not raw_text:
        fb_dict = {
            "model": config.REFLECTION_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": config.REFLECTION_TEMPERATURE,
        }
        raw_text = await _summary_fallback(fb_dict, "REFLECTION")
    parsed = await try_parse_or_repair_json(raw_text)
    return parsed.get("thoughts", []) if parsed else []

async def process_user_input(user_text, state_manager, memory_context=None):
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
        if memory_context:
            memory_context_block = f"<MEMORY_CONTEXT>\n{memory_context}\n</MEMORY_CONTEXT>"
        
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
        alarms_block = f"🔔 Твои будильники. Не выполняй их, пока не пришло время — будильник сам тебя разбудит:{missed_note}\n" + "\n".join(lines)

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
    
    raw_text = await safe_generate_content(prompt)
    parsed_json = await try_parse_or_repair_json(raw_text)
    
    if parsed_json:
        logger.info(f"📥 [GEMINI] {len(prompt)} → {len(raw_text or '')} симв {parsed_json}")
    return parsed_json