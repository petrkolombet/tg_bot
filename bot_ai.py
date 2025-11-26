# --- START OF FILE bot_ai.py ---

import logging
import asyncio
import json
import re
import google.generativeai as genai

import config

logger = logging.getLogger(__name__)

model = None
current_key_index = 0
API_KEYS = []

def init_ai(api_keys_list):
    global API_KEYS, current_key_index
    API_KEYS = api_keys_list
    current_key_index = 0
    return initialize_model()
    
def initialize_model():
    global model, current_key_index
    if not API_KEYS:
        logger.critical("❌ API_KEYS пуст!")
        return False
    current_key = API_KEYS[current_key_index]
    
    # Настройки безопасности: отключаем блокировки
    safety_settings = {
        'HARM_CATEGORY_HARASSMENT': 'BLOCK_NONE',
        'HARM_CATEGORY_HATE_SPEECH': 'BLOCK_NONE',
        'HARM_CATEGORY_SEXUALLY_EXPLICIT': 'BLOCK_NONE',
        'HARM_CATEGORY_DANGEROUS_CONTENT': 'BLOCK_NONE'
    }
    
    genai.configure(api_key=current_key, transport='rest')
    try:
        model = genai.GenerativeModel('gemini-flash-latest', safety_settings=safety_settings)
        logger.info(f"🔑 Успешная инициализация ключа #{current_key_index+1}")
        return True
    except Exception:
        try:
            model = genai.GenerativeModel('gemini-1.5-flash', safety_settings=safety_settings)
            logger.info(f"🔑 Успешная инициализация ключа #{current_key_index+1} с gemini-1.5-flash")
            return True
        except Exception as e:
            logger.error(f"❌ Не удалось инициализировать модель: {e}")
            return False

def rotate_key():
    global current_key_index
    current_key_index = (current_key_index + 1) % len(API_KEYS)
    logger.warning("⚠️ Меняю API ключ...")
    return initialize_model()

def clean_json_response(text):
    match = re.search(r'\{.*\}', text, re.DOTALL)
    return match.group(0).strip() if match else text.strip()

async def safe_generate_content(prompt, temperature=0.85):
    """
    Безопасная обертка для вызова Gemini API.
    """
    global model
    logger.info(f"📤 [GEMINI] Отправка промпта длиной: {len(prompt)} символов")
    config_gen = genai.types.GenerationConfig(temperature=temperature)
    
    for _ in range(len(API_KEYS) + 1):
        if not model:
            logger.error("❌ Модель не инициализирована.")
            return None
        try:
            loop = asyncio.get_running_loop()
            response = await loop.run_in_executor(None, lambda: model.generate_content(prompt, generation_config=config_gen))
            
            if not response.candidates:
                block_reason = "Неизвестно"
                if response.prompt_feedback:
                    block_reason = response.prompt_feedback.block_reason.name
                logger.error(f"❌ [GEMINI] Ответ заблокирован! Причина: {block_reason}")
                raise ValueError(f"Blocked: {block_reason}")
                
            return response
        except Exception as e:
            logger.error(f"❌ [GEMINI] Ошибка API с ключом #{current_key_index+1}: {e}", exc_info=False)
            if not rotate_key():
                logger.critical("❌ [GEMINI] Все ключи нерабочие.")
                return None
    return None

async def try_parse_or_repair_json(raw_response_obj):
    if not raw_response_obj: return None
    
    text_content = ""
    try:
        text_content = raw_response_obj.text
    except ValueError:
        logger.warning("⚠️ [JSON] Ответ API пуст (нет валидных частей текста). Пропускаю.")
        return None
    except Exception as e:
        logger.error(f"⚠️ [JSON] Неизвестная ошибка при чтении текста: {e}")
        return None
        
    if not text_content: return None

    try:
        return json.loads(clean_json_response(text_content))
    except (json.JSONDecodeError, AttributeError, ValueError):
        logger.warning(f"⚠️ [JSON] Ошибка парсинга. Запускаю Аварийное Восстановление JSON.")
        repair_prompt = f"Ответ AI содержит ошибку в JSON. Исправь его. ВАЖНО: Ответ должен быть в поле 'replies': ['текст']. Не используй поле 'text' для ответа. Вот нерабочий ответ:\n\n{text_content}"
        repaired_response = await safe_generate_content(repair_prompt, temperature=0.0)
        
        if repaired_response:
            try:
                repaired_text = repaired_response.text
                repaired_json = json.loads(clean_json_response(repaired_text))
                logger.info("✅ [JSON] Аварийное Восстановление JSON успешно!")
                return repaired_json
            except Exception:
                 logger.error(f"❌ [JSON] Аварийное Восстановление НЕ удалось.")
    return None

async def retrieve_memory(user_query, query_topic, state_manager):
    logger.info(f"🧠 [MEMORY] Запущен процесс воспоминания по теме: '{query_topic}'")
    
    reflection_history = state_manager.state.get("reflection_history", [])
    keywords = query_topic.split()
    
    scored_messages = []
    for msg in reflection_history:
        score = sum(1 for keyword in keywords if keyword.lower() in msg['content'].lower())
        if score > 0: scored_messages.append((score, msg))
            
    scored_messages.sort(key=lambda x: x[0], reverse=True)
    relevant_context = [msg for score, msg in scored_messages[:20]]
    memory_packet_text = "\n".join([f"{m['role']}: {m['content']}" for m in relevant_context])
    
    memory_context = f"ВНИМАНИЕ! Это приоритетная задача. Пользователь просит тебя что-то вспомнить. Вот контекст из памяти:\n---\n{memory_packet_text}\n---\nТвоя задача — изучить контекст и ответить на вопрос: '{user_query}'. Следуй своему характеру. Если ответа нет, честно признайся."
    
    return await process_user_input(user_query, state_manager, memory_context=memory_context)

async def generate_reflection(state_manager):
    logger.info("💡 [REFLECTION] Запускаю процесс гибридной рефлексии...")
    recent_history = state_manager.state["chat_history"][-40:]
    recent_history_text = "\n".join([f"{'Юзер' if m['role']=='user' else 'Бот'}: {m['content']}" for m in recent_history])
    
    reflection_history = state_manager.state["reflection_history"]
    if len(reflection_history) < 50: return []

    older_context_end_index = max(0, len(reflection_history) - len(recent_history))
    older_context_start_index = max(0, older_context_end_index - 200)
    older_context = reflection_history[older_context_start_index:older_context_end_index]
    older_context_text = "\n".join([f"{'Юзер' if m['role']=='user' else 'Бот'}: {m['content']}" for m in older_context])
    
    prompt = f'<SYSTEM_REFLECT>Ты — ИИ-аналитик. Найди связи между НЕДАВНИМ и СТАРЫМ диалогом. Сгенерируй 1-2 "фоновые мысли" (наблюдения, шутки, темы для разговора). Верни JSON-список строк.</SYSTEM_REFLECT><RECENT_HISTORY>{recent_history_text}</RECENT_HISTORY><OLDER_CONTEXT>{older_context_text}</OLDER_CONTEXT><JSON_OUTPUT>{{"thoughts": ["текст мысли"]}}</JSON_OUTPUT>'
    
    raw_response_obj = await safe_generate_content(prompt, temperature=0.7)
    parsed = await try_parse_or_repair_json(raw_response_obj)
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
    history = "\n".join([f"{'Юзер' if m['role']=='user' else 'Ты'}: {m['content']}" for m in state_manager.state["chat_history"]])
    
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
    
    raw_response_obj = await safe_generate_content(prompt)
    parsed_json = await try_parse_or_repair_json(raw_response_obj)
    
    if parsed_json: 
        logger.info(f"📥 [DECISION] {parsed_json}")
    return parsed_json