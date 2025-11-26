# --- START OF FILE bot_handlers.py ---

import logging
import asyncio
import random
import datetime
from datetime import timezone
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import ContextTypes

import config

logger = logging.getLogger(__name__)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state_manager = context.bot_data["state_manager"]
    process_user_input = context.bot_data["process_user_input"]
    retrieve_memory = context.bot_data["retrieve_memory"]
    
    if not update.message or not update.message.text or update.effective_user.id != config.ALLOWED_USER_ID: 
        return
        
    user_id = update.effective_user.id
    user_text = update.message.text
    logger.info(f"💬-> {user_text}")
    
    await state_manager.check_and_apply_peak_decay()
    await state_manager.add_history("user", user_text)
    await state_manager.update_interaction()
    
    await context.bot.send_chat_action(chat_id=user_id, action=ChatAction.TYPING)
    
    initial_decision = await process_user_input(user_text, state_manager)
    
    if initial_decision and (memory_topic := initial_decision.get("memory_query_topic")):
        decision = await retrieve_memory(user_text, memory_topic, state_manager)
    else:
        decision = initial_decision
        
    if not decision: 
        await update.message.reply_text(random.choice(config.FALLBACK_PHRASES))
        return
        
    if decision.get("forgive"): await state_manager.set_offense_state(False)
    elif decision.get("is_offended"): await state_manager.set_offense_state(True)
    if ignored_keywords := decision.get("ignored_topic_keywords"): await state_manager.set_pending_topic(ignored_keywords)
    if used_thought_id := decision.get("used_thought_id"): await state_manager.remove_thought(used_thought_id)
    if (shift := float(decision.get("mood_shift", 0.0))) != 0.0: await state_manager.apply_reaction(shift)
    
    if task_data := decision.get("add_task"):
        if isinstance(task_data, dict): 
            await state_manager.add_task(
                text=task_data.get("text", "без темы"), 
                minutes=int(task_data.get("minutes", 5)), 
                priority=task_data.get("priority", "low")
            )
    
    replies = decision.get("replies") or []
    if not replies and (single_text := decision.get("text")):
        if isinstance(single_text, str) and len(single_text) > 0:
            logger.warning(f"⚠️ [JSON] Обнаружен ответ в поле 'text' вместо 'replies'. Исправляю.")
            replies = [single_text]
            
    if replies:
        for i, item in enumerate(replies):
            message_text = item if isinstance(item, str) else item.get("text", "")
            if not message_text: continue

            message_text = message_text.lower()
            logger.info(f"💡<- {message_text}")
            await state_manager.add_history("model", message_text)
            
            if random.random() < config.TYPO_CHANCE and ' ' in message_text and len(message_text) > 10:
                words = message_text.split(' '); word_idx = random.randint(0, len(words) - 1)
                if len(words[word_idx]) > 3:
                    ow = words[word_idx]; pos = random.randint(0, len(ow) - 2)
                    words[word_idx] = ow[:pos] + ow[pos+1] + ow[pos] + ow[pos+2:]
                    await update.message.reply_text(" ".join(words))
                    await asyncio.sleep(1.5)
                    await context.bot.send_message(chat_id=user_id, text=f"*{ow}")
                    continue
            
            await update.message.reply_text(message_text)
            if i < len(replies) - 1:
                await context.bot.send_chat_action(chat_id=user_id, action=ChatAction.TYPING)
                next_len = len(replies[i+1]) if isinstance(replies[i+1], str) else len(replies[i+1].get("text", ""))
                await asyncio.sleep(min(1.0 + next_len * 0.06, 4.0))
    else:
        logger.info("💡<- [молчание]")

async def background_tasks(context: ContextTypes.DEFAULT_TYPE):
    state_manager = context.bot_data["state_manager"]
    process_user_input = context.bot_data["process_user_input"]
    generate_reflection = context.bot_data["generate_reflection"]
    
    now_ts = datetime.datetime.now(timezone.utc).timestamp()
    
    try:
        if (now_ts - state_manager.state["last_interaction"]) > config.SILENCE_BEFORE_REFLECTION_HOURS * 3600 and \
           (now_ts - state_manager.state["last_reflection_time"]) > config.REFLECTION_INTERVAL_HOURS * 3600:
            thoughts = await generate_reflection(state_manager)
            await state_manager.add_thoughts(thoughts)
    except Exception:
        logger.error("💥 [CRON] Ошибка в процессе рефлексии!", exc_info=True)

    try:
        tasks = state_manager.state.get("task_list", [])
        if not tasks: return

        due_tasks = sorted([t for t in tasks if now_ts >= t.get("due_time", now_ts + 1)], key=lambda x: x.get("due_time"))
        if not due_tasks: return

        task_to_process = None
        system_trigger_text = ""
        
        high_priority_task = next((t for t in due_tasks if t.get("priority") == "high"), None)
        
        if high_priority_task:
            task_to_process = high_priority_task
            logger.info(f"⏰ [TASK] Напоминание: '{task_to_process['text']}'")
            # ЧЕТКИЙ ТРИГГЕР ДЛЯ БОТА
            system_trigger_text = f"[SYSTEM_TRIGGER: Сработало напоминание: {task_to_process['text']}]"
            
        elif (now_ts - state_manager.state["last_interaction"]) > config.SILENCE_BEFORE_PROACTIVE_MINUTES * 60:
            task_to_process = due_tasks[0]
            logger.info(f"🤔 [TASK] Follow-up: '{task_to_process['text']}'")
            system_trigger_text = f"[SYSTEM_TRIGGER: Тишина в чате. Задача из списка: {task_to_process['text']}. Начни разговор об этом.]"

        if task_to_process:
            # Отправляем триггер
            decision = await process_user_input(system_trigger_text, state_manager)
            
            replies = decision.get("replies") or []
            if not replies and (single_text := decision.get("text")):
                 if isinstance(single_text, str) and len(single_text) > 0:
                    replies = [single_text]
            
            if replies:
                for msg in replies:
                    text_to_send = msg if isinstance(msg, str) else msg.get("text", "")
                    if text_to_send:
                        await context.bot.send_message(chat_id=config.ALLOWED_USER_ID, text=text_to_send.lower())
                        await state_manager.add_history("model", text_to_send.lower())
                        await asyncio.sleep(random.uniform(1.5, 3.0))
                
                await state_manager.remove_task(task_to_process["id"])
            elif high_priority_task:
                # Если проигнорил важное - переносим на 5 мин
                logger.warning("⚠️ [TASK] Бот проигнорировал важное напоминание. Откладываю.")
                await state_manager.remove_task(task_to_process["id"])
                await state_manager.add_task(task_to_process["text"], 5, "high")
                
    except Exception:
        logger.error("💥 [CRON] Ошибка в исполнителе задач!", exc_info=True)

    try:
        await state_manager.update_physics()
    except Exception:
        logger.error("💥 [CRON] Ошибка в обновлении физики!", exc_info=True)