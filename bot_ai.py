# --- START OF FILE bot_ai.py ---

import logging
import asyncio
import json
import re
import urllib.request

import config
from rag import insert_to_rag, query_rag

logger = logging.getLogger(__name__)

def clean_json_response(text):
    match = re.search(r'\{.*\}', text, re.DOTALL)
    return match.group(0).strip() if match else text.strip()

async def safe_generate_content(prompt, temperature=0.85):
    logger.info(f"📤 [GEMINI] Отправка промпта длиной: {len(prompt)} символов")
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
            logger.info(f"📤 [GEMINI] Ответ получен, длина: {len(text)} символов")
            return text
        except Exception as e:
            logger.error(f"❌ [GEMINI] Ошибка (попытка {attempt+1}): {e}")
            if attempt < 2:
                await asyncio.sleep(2)
    return None

async def search_web(query):
    logger.info(f"🔍 [SEARCH] Поиск: {query}")
    payload = json.dumps({
        "model": config.DEEPSEEK_MODEL,
        "messages": [
            {"role": "system", "content": "Ты — поисковый агент. Отвечай КОРОТКО и ТОЛЬКО по заданию. Всегда добавляй ссылки на источники. Не пиши воду, не объясняй контекст — только факт и ссылка."},
            {"role": "user", "content": query}
        ],
        "temperature": 0.3,
        "search": True
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
    
    results = []
    r1 = await query_rag(user_query, top_k=5)
    if r1: results.append(r1)
    if query_topic:
        r2 = await query_rag(query_topic, top_k=5)
        if r2: results.append(r2)
    result = "\n---\n".join(results) if results else ""
    
    if result:
        memory_context = f"ВНИМАНИЕ! Это приоритетная задача. Пользователь просит тебя что-то вспомнить. Вот контекст из памяти:\n---\n{result}\n---\nТвоя задача — изучить контекст и ответить на вопрос: '{user_query}'. Если ты уже знаешь ответ из контекста диалога — используй его. Следуй своему характеру. Если ответа нет, честно признайся."
    else:
        memory_context = f"Пользователь спрашивает: '{user_query}'. Память пуста или произошла ошибка. Если ответа нет, честно признайся."
    
    return await process_user_input(user_query, state_manager, memory_context=memory_context)

async def generate_reflection(state_manager):
    logger.info("💡 [REFLECTION] Запускаю процесс гибридной рефлексии...")
    recent_history = state_manager.state["chat_history"][-40:]
    recent_history_text = "\n".join([f"[{m.get('ts','')}] {'Юзер' if m['role']=='user' else 'Бот'}: {m['content']}" for m in recent_history])
    
    reflection_history = state_manager.state["reflection_history"]
    if len(reflection_history) < 50: return []

    older_context_end_index = max(0, len(reflection_history) - len(recent_history))
    older_context_start_index = max(0, older_context_end_index - 200)
    older_context = reflection_history[older_context_start_index:older_context_end_index]
    older_context_text = "\n".join([f"{'Юзер' if m['role']=='user' else 'Бот'}: {m['content']}" for m in older_context])
    
    prompt = f'<SYSTEM_REFLECT>Ты — ИИ-аналитик. Найди связи между НЕДАВНИМ и СТАРЫМ диалогом. Сгенерируй 1-2 "фоновые мысли" (наблюдения, шутки, темы для разговора). Верни JSON-список строк.</SYSTEM_REFLECT><RECENT_HISTORY>{recent_history_text}</RECENT_HISTORY><OLDER_CONTEXT>{older_context_text}</OLDER_CONTEXT><JSON_OUTPUT>{{"thoughts": ["текст мысли"]}}</JSON_OUTPUT>'
    
    raw_text = await safe_generate_content(prompt, temperature=0.7)
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
    
    # --- КЛЮЧЕВАЯ ЛОГИКА: ОПРЕДЕЛЕНИЕ ТИПА ВХОДА ---
    is_system_trigger = "[SYSTEM_TRIGGER:" in user_text
    
    if is_system_trigger:
        # Извлекаем текст задачи и очищаем user_text, чтобы бот не путал это с сообщением юзера
        clean_task_text = user_text.replace("[SYSTEM_TRIGGER:", "").replace("]", "").strip()
        task_execution_block = f"<TASK_EXECUTION>\nПРИШЛО ВРЕМЯ ВЫПОЛНИТЬ ЗАДАЧУ/НАПОМИНАНИЕ:\n'{clean_task_text}'\nСформулируй сообщение пользователю об этом прямо сейчас.\n</TASK_EXECUTION>"
        user_text = "" # Очистка ввода, так как это системный вызов
        logger.info(f"⚙️ [AI] Режим выполнения задачи: {clean_task_text}")
    else:
        # Обычная обработка сообщений пользователя
        if memory_context:
            memory_context_block = f"<MEMORY_CONTEXT>\n{memory_context}\n</MEMORY_CONTEXT>"
        elif triggered_topic := state_manager.get_and_clear_pending_topic(user_text):
            system_alert = f"<SYSTEM_ALERT>ВНИМАНИЕ: Пользователь второй раз вернулся к теме '{', '.join(triggered_topic)}'. Прояви интерес.</SYSTEM_ALERT>"
        elif state_manager.is_offended():
            system_alert = "<SYSTEM_ALERT>ВНИМАНИЕ: Ты обижен. Отвечай холодно/односложно, либо молчи. Если юзер извиняется, можешь простить (`forgive: true`).</SYSTEM_ALERT>"
        
    mood_instr = state_manager.get_mood_instruction()
    history = "\n".join([f"[{m.get('ts','')}] {'Юзер' if m['role']=='user' else 'Ты'}: {m['content']}" for m in state_manager.state["chat_history"]])
    
    thoughts_block = ""
    if state_manager.state["background_thoughts"]:
        thoughts_text = "\n".join([f'- ({t["id"]}) {t["text"]}' for t in state_manager.state["background_thoughts"]])
        thoughts_block = f'<BACKGROUND_THOUGHTS>Твои фоновые мысли. ПРАВИЛО: Если разговор затухает, используй мысль.\nТвои текущие мысли:\n{thoughts_text}</BACKGROUND_THOUGHTS>'
    
    existing_tasks_list = state_manager.state.get("task_list", [])
    existing_tasks_str = "\n".join([f"- [{t['priority']}] {t.get('text', 'без темы')}" for t in existing_tasks_list])
    if not existing_tasks_str: existing_tasks_str = "Список пуст."
        
    prompt = prompt_template.format(
        memory_context_block=memory_context_block, 
        system_alert=system_alert, 
        msk_time=state_manager.get_msk_time_obj().strftime("%H:%M"), 
        mood_instr=mood_instr, 
        thoughts_block=thoughts_block, 
        history=history,
        task_execution_block=task_execution_block, # Вставляем блок выполнения задачи
        user_text=user_text, 
        existing_tasks=existing_tasks_str
    )
    
    raw_text = await safe_generate_content(prompt)
    parsed_json = await try_parse_or_repair_json(raw_text)
    
    if parsed_json: 
        logger.info(f"📥 [DECISION] {parsed_json}")
    return parsed_json