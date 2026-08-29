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
from rag import insert_to_rag
import server_access
from bot_ai import summarize_tool_output

logger = logging.getLogger(__name__)

async def _typing_loop(bot, user_id, stop_event):
    while not stop_event.is_set():
        try:
            await bot.send_chat_action(chat_id=user_id, action=ChatAction.TYPING)
        except Exception:
            pass
        await asyncio.sleep(4)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state_manager = context.bot_data["state_manager"]
    process_user_input = context.bot_data["process_user_input"]
    retrieve_memory = context.bot_data["retrieve_memory"]
    search_web = context.bot_data["search_web"]
    
    if not update.message or not update.message.text or update.effective_user.id != config.ALLOWED_USER_ID: 
        return
        
    user_id = update.effective_user.id
    user_text = update.message.text
    logger.info(f"💬-> {user_text}")
    
    stop_typing = asyncio.Event()
    typing_task = asyncio.create_task(_typing_loop(context.bot, user_id, stop_typing))
    
    try:
        await state_manager.check_and_apply_peak_decay()
        await state_manager.add_history("user", user_text)
        asyncio.create_task(insert_to_rag(f"Пользователь: {user_text}", metadata=f"user_{user_id}"))
        await state_manager.update_interaction()
        
        initial_decision = await process_user_input(user_text, state_manager)
        
        if initial_decision and (memory_topic := initial_decision.get("memory_query_topic")):
            decision = await retrieve_memory(user_text, memory_topic, state_manager)
            if decision and decision.get("memory_query_topic") and not decision.get("replies"):
                logger.info("🔄 [MEMORY] Вторая попытка воспоминания...")
                decision = await retrieve_memory(user_text, decision["memory_query_topic"], state_manager)
                if decision and decision.get("memory_query_topic") and not decision.get("replies"):
                    logger.info("❌ [MEMORY] Две попытки, ничего не нашлось — отправляю 'ничего не нашлось'")
                    memory_context = f"Пользователь просил вспомнить: '{user_text}'. В памяти ничего не нашлось. Честно скажи что не помнишь."
                    decision = await process_user_input(user_text, state_manager, memory_context=memory_context)
        elif initial_decision and initial_decision.get("google_search"):
            search_query = initial_decision["google_search"]
            ack_replies = initial_decision.get("replies") or []
            if ack_replies:
                ack_text = ack_replies[0] if isinstance(ack_replies[0], str) else ack_replies[0].get("text", "")
                if ack_text:
                    await update.message.reply_text(ack_text.lower())
                    await state_manager.add_history("model", ack_text.lower())
            logger.info(f"🔍 [SEARCH] Запрос к поиску: {search_query}")
            search_result = await search_web(search_query)
            # В историю — только факт поиска, результат не пишем (одноразовая справка)
            await state_manager.add_tool_record(f'🌐 искал: "{search_query}"')
            if search_result:
                search_context = f"Результат поиска по запросу '{search_query}':\n---\n{search_result}\n---"
                decision = await process_user_input(user_text, state_manager, memory_context=search_context)
            else:
                decision = initial_decision
        elif initial_decision and initial_decision.get("server_command"):
            server_spec = initial_decision["server_command"]
            server_cmd = server_spec.get("cmd") if isinstance(server_spec, dict) else server_spec
            server_desc = server_spec.get("desc", "") if isinstance(server_spec, dict) else ""
            ack_replies = initial_decision.get("replies") or []
            if ack_replies:
                ack_text = ack_replies[0] if isinstance(ack_replies[0], str) else ack_replies[0].get("text", "")
                if ack_text:
                    await update.message.reply_text(ack_text.lower())
                    await state_manager.add_history("model", ack_text.lower())
            logger.info(f"🖥️ [SERVER] Команда: {server_cmd} ({server_desc})")
            result = await server_access.execute(server_cmd, timeout=30)
            output = result.get("output", "")
            error = result.get("error", "")
            logger.info(f"🖥️ [SERVER] output={len(output)} символов")
            server_context = f"Результат выполнения команды '{server_cmd}':\n---\n{output}\n---"
            if error:
                server_context += f"\nОшибки:\n{error}"
            # Запись в историю: короткий результат — как есть, длинный — выжимка дипсика
            if len(output) <= config.TOOL_RESULT_LIMIT:
                report = output
            else:
                report = await summarize_tool_output(server_cmd, server_desc, output, state_manager)
            rec = f'⚙️ выполнил: {server_cmd} → {report}'
            await state_manager.add_tool_record(rec)
            decision = await process_user_input(user_text, state_manager, memory_context=server_context)
        else:
            decision = initial_decision
        
        stop_typing.set()
        
        if not decision: 
            await update.message.reply_text(random.choice(config.FALLBACK_PHRASES))
            return
            
        if used_thought_id := decision.get("used_thought_id"): await state_manager.remove_thought(used_thought_id)
        if (shift := float(decision.get("mood_shift", 0.0))) != 0.0: await state_manager.apply_reaction(shift)
        
        if reaction := decision.get("reaction"):
            try:
                from telegram import ReactionTypeEmoji
                await update.message.set_reaction([ReactionTypeEmoji(reaction)])
            except Exception as e:
                logger.warning(f"⚠️ [REACTION] Ошибка: {e}")
        
        if alarm_data := decision.get("add_alarm"):
            if isinstance(alarm_data, dict):
                context = state_manager.build_context_snippet(config.ALARM_CONTEXT_SNIPPET)
                await state_manager.add_alarm(
                    text=alarm_data.get("text", "без темы"),
                    minutes=int(alarm_data.get("minutes", 60)),
                    context=context
                )
        
        if interest_text := decision.get("add_interest"):
            if isinstance(interest_text, str) and interest_text.strip():
                await state_manager.add_interest(interest_text.strip())
        
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
                asyncio.create_task(insert_to_rag(f"Бот: {message_text}", metadata=f"bot_{user_id}"))
                
                await update.message.reply_text(message_text)
                if i < len(replies) - 1:
                    await context.bot.send_chat_action(chat_id=user_id, action=ChatAction.TYPING)
                    next_len = len(replies[i+1]) if isinstance(replies[i+1], str) else len(replies[i+1].get("text", ""))
                    await asyncio.sleep(min(1.0 + next_len * 0.06, 3.0))
        else:
            logger.info("💡<- [молчание]")
    finally:
        stop_typing.set()

async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state_manager = context.bot_data["state_manager"]
    process_user_input = context.bot_data["process_user_input"]
    retrieve_memory = context.bot_data["retrieve_memory"]
    search_web = context.bot_data["search_web"]
    transcribe_voice = context.bot_data["transcribe_voice"]
    
    if not update.message or not update.message.voice or update.effective_user.id != config.ALLOWED_USER_ID:
        return
    
    user_id = update.effective_user.id
    logger.info(f"🎤-> Голосовое получено")
    
    stop_typing = asyncio.Event()
    typing_task = asyncio.create_task(_typing_loop(context.bot, user_id, stop_typing))
    
    try:
        voice = update.message.voice
        file = None
        for attempt in range(3):
            try:
                file = await context.bot.get_file(voice.file_id)
                break
            except Exception as e:
                logger.warning(f"⚠️ [VOICE] get_file попытка {attempt+1}: {e}")
                if attempt < 2:
                    await asyncio.sleep(2)
        if not file:
            await update.message.reply_text("не скачал голосовое, повтори")
            return
        audio_bytes = await file.download_as_bytearray()
        
        text = await transcribe_voice(bytes(audio_bytes), f"voice_{voice.file_id}.ogg")
        if not text:
            await update.message.reply_text("не распознал, повтори")
            return
        
        logger.info(f"🎤-> Текст: {text}")
        await state_manager.add_history("user", f"[расшифровка голосового]: {text}")
        asyncio.create_task(insert_to_rag(f"Пользователь [расшифровка голосового]: {text}", metadata=f"user_{user_id}"))
        await state_manager.update_interaction()
        
        initial_decision = await process_user_input(f"[расшифровка голосового]: {text}", state_manager)
        
        if initial_decision and (memory_topic := initial_decision.get("memory_query_topic")):
            decision = await retrieve_memory(text, memory_topic, state_manager)
            if decision and decision.get("memory_query_topic") and not decision.get("replies"):
                decision = await retrieve_memory(text, decision["memory_query_topic"], state_manager)
                if decision and decision.get("memory_query_topic") and not decision.get("replies"):
                    memory_context = f"Пользователь просил вспомнить: '{text}'. В памяти ничего не нашлось. Честно скажи что не помнишь."
                    decision = await process_user_input(text, state_manager, memory_context=memory_context)
        elif initial_decision and initial_decision.get("google_search"):
            search_query = initial_decision["google_search"]
            ack_replies = initial_decision.get("replies") or []
            if ack_replies:
                ack_text = ack_replies[0] if isinstance(ack_replies[0], str) else ack_replies[0].get("text", "")
                if ack_text:
                    await update.message.reply_text(ack_text.lower())
                    await state_manager.add_history("model", ack_text.lower())
            search_result = await search_web(search_query)
            await state_manager.add_tool_record(f'🌐 искал: "{search_query}"')
            if search_result:
                search_context = f"Результат поиска по запросу '{search_query}':\n---\n{search_result}\n---"
                decision = await process_user_input(text, state_manager, memory_context=search_context)
            else:
                decision = initial_decision
        elif initial_decision and initial_decision.get("server_command"):
            server_spec = initial_decision["server_command"]
            server_cmd = server_spec.get("cmd") if isinstance(server_spec, dict) else server_spec
            server_desc = server_spec.get("desc", "") if isinstance(server_spec, dict) else ""
            ack_replies = initial_decision.get("replies") or []
            if ack_replies:
                ack_text = ack_replies[0] if isinstance(ack_replies[0], str) else ack_replies[0].get("text", "")
                if ack_text:
                    await update.message.reply_text(ack_text.lower())
                    await state_manager.add_history("model", ack_text.lower())
            logger.info(f"🖥️ [SERVER] Команда: {server_cmd} ({server_desc})")
            result = await server_access.execute(server_cmd, timeout=30)
            output = result.get("output", "")
            error = result.get("error", "")
            logger.info(f"🖥️ [SERVER] output={len(output)} символов")
            server_context = f"Результат выполнения команды '{server_cmd}':\n---\n{output}\n---"
            if error:
                server_context += f"\nОшибки:\n{error}"
            if len(output) <= config.TOOL_RESULT_LIMIT:
                report = output
            else:
                report = await summarize_tool_output(server_cmd, server_desc, output, state_manager)
            rec = f'⚙️ выполнил: {server_cmd} → {report}'
            await state_manager.add_tool_record(rec)
            decision = await process_user_input(text, state_manager, memory_context=server_context)
        else:
            decision = initial_decision
        
        stop_typing.set()
        
        if not decision:
            await update.message.reply_text(random.choice(config.FALLBACK_PHRASES))
            return
        
        if used_thought_id := decision.get("used_thought_id"): await state_manager.remove_thought(used_thought_id)
        if (shift := float(decision.get("mood_shift", 0.0))) != 0.0: await state_manager.apply_reaction(shift)
        
        if reaction := decision.get("reaction"):
            try:
                from telegram import ReactionTypeEmoji
                await update.message.set_reaction([ReactionTypeEmoji(reaction)])
            except Exception as e:
                logger.warning(f"⚠️ [REACTION] Ошибка: {e}")
        
        replies = decision.get("replies") or []
        if replies:
            for i, item in enumerate(replies):
                message_text = item if isinstance(item, str) else item.get("text", "")
                if not message_text: continue
                message_text = message_text.lower()
                logger.info(f"💡<- {message_text}")
                await state_manager.add_history("model", message_text)
                asyncio.create_task(insert_to_rag(f"Бот: {message_text}", metadata=f"bot_{user_id}"))
                await update.message.reply_text(message_text)
                if i < len(replies) - 1:
                    await context.bot.send_chat_action(chat_id=user_id, action=ChatAction.TYPING)
                    next_len = len(replies[i+1]) if isinstance(replies[i+1], str) else len(replies[i+1].get("text", ""))
                    await asyncio.sleep(min(1.0 + next_len * 0.06, 3.0))
        else:
            logger.info("💡<- [молчание]")
    finally:
        stop_typing.set()

async def background_tasks(context: ContextTypes.DEFAULT_TYPE):
    state_manager = context.bot_data["state_manager"]
    process_user_input = context.bot_data["process_user_input"]
    generate_reflection = context.bot_data["generate_reflection"]
    update_longterm_summary = context.bot_data["update_longterm_summary"]
    
    now_ts = datetime.datetime.now(timezone.utc).timestamp()

    # Обновление долгосрочного саммари каждые 50 сообщений
    try:
        if state_manager.state.get("messages_since_summary", 0) >= 50:
            await update_longterm_summary(state_manager)
    except Exception:
        logger.error("💥 [CRON] Ошибка в обновлении саммари!", exc_info=True)
    
    try:
        if (now_ts - state_manager.state["last_interaction"]) > config.SILENCE_BEFORE_REFLECTION_HOURS * 3600 and \
           (now_ts - state_manager.state["last_reflection_time"]) > config.REFLECTION_INTERVAL_HOURS * 3600:
            thoughts = await generate_reflection(state_manager)
            await state_manager.add_thoughts(thoughts)
    except Exception:
        logger.error("💥 [CRON] Ошибка в процессе рефлексии!", exc_info=True)

    try:
        alarms = state_manager.state.get("alarms", [])
        due_alarms = sorted([a for a in alarms if now_ts >= a.get("due_ts", now_ts + 1)], key=lambda x: x.get("due_ts"))
        if due_alarms:
            alarm = due_alarms[0]
            alarm_text = alarm["text"]
            ctx = alarm.get("context", "")
            ctx_block = f"\nКонтекст беседы, когда ты его ставил:\n{ctx}" if ctx else ""
            logger.info(f"⏰ [ALARM] Сработал будильник: '{alarm_text}'")
            trigger = (
                f"[SYSTEM_TRIGGER: ⏰ НАПОМИНАНИЕ: {alarm_text}. "
                f"Ты поставил себе это напоминание.{ctx_block}\n"
                f"Проверь актуальность по истории: договорились не писать в это время, "
                f"тема устарела, или ты уже спрашивал об этом. Если не актуально — верни replies [] и молчи. "
                f"Пиши пользователю, если действительно ничего не мешает и нет причин молчать.]"
            )
            decision = await process_user_input(trigger, state_manager)

            replies = decision.get("replies") or []
            if not replies and (single_text := decision.get("text")):
                if isinstance(single_text, str) and len(single_text) > 0:
                    logger.warning(f"⚠️ [JSON] Обнаружен ответ в поле 'text' вместо 'replies'. Исправляю.")
                    replies = [single_text]

            if replies:
                for msg in replies:
                    text_to_send = msg if isinstance(msg, str) else msg.get("text", "")
                    if text_to_send:
                        await context.bot.send_message(chat_id=config.ALLOWED_USER_ID, text=text_to_send.lower())
                        await state_manager.add_history("model", text_to_send.lower())
                        await asyncio.sleep(random.uniform(1.5, 3.0))
                await state_manager.remove_alarm(alarm["id"])
            else:
                logger.info(f"⏰ [ALARM] Бот промолчал по будильнику '{alarm_text}' — помечаю пропущенным.")
                await state_manager.bump_alarm_missed(alarm)
            return

        # Темы (мягкие намерения) — раз в 30 минут, только в затяжной тишине
        can_followup, reason = await state_manager.try_interest_followup(
            cooldown_minutes=config.INTEREST_FOLLOWUP_COOLDOWN_MINUTES,
            silence_minutes=config.SILENCE_BEFORE_PROACTIVE_MINUTES,
            now_ts=now_ts
        )
        if can_followup:
            topics = [f"- {t['text']} [id:{t['id']}]" for t in state_manager.state["interests"]]
            topics_str = "\n".join(topics)
            logger.info("🤔 [INTEREST] Follow-up тишины (все темы за один вызов)")
            trigger = (
                f"[SYSTEM_TRIGGER: Тишина в чате. Вот все твои темы, которые ты хотел поднять:\n{topics_str}\n"
                f"Выбери одну, которая сейчас уместнее всего, и напиши пользователю про неё. "
                f"Если ничего не актуально — верни replies [].]"
            )
            decision = await process_user_input(trigger, state_manager)
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
            logger.info(f"🤔 [INTEREST] Попытка завершена ({len(replies)} сообщений), фиксирую кулдаун.")
            await state_manager.mark_interest_attempt()
        elif interests_exist := bool(state_manager.state.get("interests")):
            logger.info(f"🤔 [INTEREST] Follow-up пропущен: {reason}")
            _ = interests_exist

    except Exception:
        logger.error("💥 [CRON] Ошибка в исполнителе задач!", exc_info=True)

async def handle_server(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /server — выполнение команд на сервере."""
    if not update.message or update.effective_user.id != config.ALLOWED_USER_ID:
        return

    cmd_text = update.message.text.replace("/server", "").strip()
    if not cmd_text:
        await update.message.reply_text(
            "команда: /server <shell-команда>\n\n"
            "workspace (/root/tg_bot/workspace) — чтение/запись/запуск\n"
            "вне workspace — только чтение и мониторинг"
        )
        return

    await update.message.reply_text(f"⏳ выполняю: `{cmd_text}`", parse_mode="Markdown")

    result = await server_access.execute(cmd_text, timeout=30)

    mode_icon = {"write": "📝", "read": "📖", "monitor": "📊", "denied": "🚫"}.get(result["mode"], "❓")
    header = f"{mode_icon} `{result['mode']}`"

    output = result.get("output", "")
    error = result.get("error", "")

    parts = [header]
    if output:
        parts.append(f"```\n{output}\n```")
    if error:
        parts.append(f"⚠️\n```\n{error}\n```")

    response = "\n".join(parts)

    # Ограничиваем длину сообщения Telegram (4096 символов)
    if len(response) > 4000:
        response = response[:4000] + "\n... (обрезано)"

    await update.message.reply_text(response, parse_mode="Markdown")