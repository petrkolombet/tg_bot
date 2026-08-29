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
from bot_ai import summarize_tool_output, summarize_search_result

logger = logging.getLogger(__name__)

async def _typing_loop(bot, user_id, stop_event):
    while not stop_event.is_set():
        try:
            await bot.send_chat_action(chat_id=user_id, action=ChatAction.TYPING)
        except Exception:
            pass
        await asyncio.sleep(4)

def _flatten_replies(replies):
    """Разворачивает replies любой вложенности в плоский список строк.
    Модель иногда присылает [['a','b','c']] вместо ['a','b','c'] — разбираем штатно."""
    out = []
    def walk(item):
        if isinstance(item, str):
            if item.strip():
                out.append(item)
        elif isinstance(item, dict):
            t = item.get("text")
            if isinstance(t, str) and t.strip():
                out.append(t)
        elif isinstance(item, (list, tuple)):
            for x in item:
                walk(x)
        elif item is not None:
            s = str(item)
            if s.strip():
                out.append(s)
    walk(replies)
    return out

async def _handle_alarm_action(alarm_data, state_manager):
    """Выполняет add_alarm action (add/remove/edit).
    Возвращает (context_text, record_text):
      context_text — результат для модели (койтext следующего хода);
      record_text — запись в историю (role=tool)."""
    action = alarm_data.get("action", "add")
    alarm_text = alarm_data.get("text", "без темы")
    alarm_minutes = alarm_data.get("minutes")
    alarm_id = alarm_data.get("id")

    if action == "remove":
        if alarm_id:
            ok = await state_manager.remove_alarm(alarm_id)
            target = f"id {alarm_id}"
        else:
            ok = await state_manager.remove_alarm_by_text(alarm_text)
            target = f"'{alarm_text}'"
        status = "удалён" if ok else "не найден — ничего не удалено"
        return (
            f"Результат удаления будильника {target}: {status}.",
            f"🗑️ удалил будильник {target} → {'ок' if ok else 'не найден'}",
            ok,
        )

    if action == "edit":
        if alarm_id:
            ok = await state_manager.edit_alarm(
                alarm_id,
                text=alarm_text if alarm_text != "без темы" else None,
                minutes=alarm_minutes,
            )
            target = f"id {alarm_id}"
        else:
            ok = await state_manager.edit_alarm_by_text(alarm_text, minutes=alarm_minutes)
            target = f"'{alarm_text}'"
        status = "изменён" if ok else "не найден — ничего не изменено"
        return (
            f"Результат изменения будильника {target}: {status}.",
            f"✏️ изменил будильник {target} → {'ок' if ok else 'не найден'}",
            ok,
        )

    # add: id от модели игнорируем, генерим свой в add_alarm
    context_snippet = state_manager.build_context_snippet(config.ALARM_CONTEXT_SNIPPET)
    new_id = await state_manager.add_alarm(
        text=alarm_text,
        minutes=int(alarm_minutes or 60),
        context=context_snippet,
    )
    minutes = int(alarm_minutes or 60)
    if new_id:
        status = f"поставлен (id {new_id}), сработает через {minutes} мин"
    else:
        status = "не создан: такой будильник уже есть"
    return (
        f"Результат создания будильника '{alarm_text}': {status}.",
        f"🔔 поставил будильник '{alarm_text}' → {status}",
        bool(new_id),
    )

async def _tool_loop(update, context, state_manager, user_text, initial_decision):
    """Универсальный цикл выполнения инструментов: server_command, google_search,
    memory_query_topic. Повторяем, пока модель не ответит текстом.
    Правила:
      - не повторять ТОТ ЖЕ инструмент 1-в-1 сразу после него (через другой — можно);
      - максимум MAX_TOOLS_TURN инструментов подряд — далее принудительный текстовый ответ;
      - жёсткий потолок MAX_TOOLS_TURN+2 итерации — принудительный выход из цикла."""
    process_user_input = context.bot_data["process_user_input"]
    retrieve_memory = context.bot_data["retrieve_memory"]
    search_web = context.bot_data["search_web"]

    decision = initial_decision
    last_key = None
    loop_count = 0

    def wants_tool(d):
        return (
            d and isinstance(d, dict)
            and (d.get("server_command") or d.get("google_search") or d.get("memory_query_topic")
                 or d.get("add_alarm"))
        )

    def tool_key(d):
        if d.get("server_command"):
            s = d["server_command"]
            cmd = s.get("cmd") if isinstance(s, dict) else s
            return ("server", cmd)
        if d.get("google_search"):
            return ("search", d["google_search"])
        if d.get("memory_query_topic"):
            return ("memory", d["memory_query_topic"])
        if d.get("add_alarm"):
            a = d["add_alarm"]
            if isinstance(a, dict):
                act = a.get("action", "add")
                ident = a.get("id") or a.get("text", "")
                return ("alarm", f"{act}:{ident}")
            return ("alarm", f"{a}")
        return None

    while wants_tool(decision):
        cur_key = tool_key(decision)
        loop_count += 1

        # Жёсткий потолок — даже принудительные запросы не крутим вечно
        if loop_count > config.MAX_TOOLS_TURN + 2:
            logger.warning(f"⚠️ [TOOLS] Полный обрыв цикла после {loop_count} итераций")
            break

        if loop_count > config.MAX_TOOLS_TURN:
            logger.warning(f"⚠️ [TOOLS] Превышен лимит {config.MAX_TOOLS_TURN} инструментов подряд")
            forced = (
                f"⚠️ Ты выполнил уже {config.MAX_TOOLS_TURN} инструментов подряд (команды/поиск/воспоминания). "
                "Хватит — ответь пользователю обычным текстом, используя последние результаты. "
                "Новых инструментов не запускай."
            )
            decision = await process_user_input(
                user_text, state_manager, memory_context=forced
            )
            continue

        if cur_key == last_key:
            logger.warning(f"⚠️ [TOOLS] ДУБЛИКАТ инструмента (блокирую): {cur_key}")
            blocked_result = (
                f"⚠️ Не выполнено: ты уже делал это ({cur_key[0]}: '{cur_key[1]}') прямо перед этим. "
                "Не повторяй сразу же то же самое. Если нужен свежий результат — используй другой "
                "инструмент или просто ответь текстом."
            )
            decision = await process_user_input(
                user_text, state_manager, memory_context=blocked_result
            )
            continue

        last_key = cur_key

        if decision.get("server_command"):
            server_spec = decision["server_command"]
            server_cmd = server_spec.get("cmd") if isinstance(server_spec, dict) else server_spec
            server_desc = server_spec.get("desc", "") if isinstance(server_spec, dict) else ""

            ack_replies = _flatten_replies(decision.get("replies"))
            if ack_replies:
                ack_text = ack_replies[0]
                await update.message.reply_text(ack_text)
                await state_manager.add_history("model", ack_text)

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

            decision = await process_user_input(
                user_text, state_manager, memory_context=server_context
            )

        elif decision.get("google_search"):
            search_query = decision["google_search"]
            ack_replies = _flatten_replies(decision.get("replies"))
            if ack_replies:
                ack_text = ack_replies[0]
                await update.message.reply_text(ack_text)
                await state_manager.add_history("model", ack_text)
            logger.info(f"🔍 [SEARCH] Запрос к поиску: {search_query}")
            search_result = await search_web(search_query)
            if search_result:
                # В память — выжимка (или целиком, если коротко), как у команд
                if len(search_result) <= config.TOOL_RESULT_LIMIT:
                    search_report = search_result
                else:
                    search_report = await summarize_search_result(search_query, search_result, state_manager)
                await state_manager.add_tool_record(f'🌐 искал: "{search_query}" → {search_report}')
                search_context = f"Результат поиска по запросу '{search_query}':\n---\n{search_result}\n---"
                decision = await process_user_input(user_text, state_manager, memory_context=search_context)
            else:
                await state_manager.add_tool_record(f'🌐 искал: "{search_query}" (ничего не нашёл)')
                empty_search = (
                    f"⚠️ Поиск по запросу '{search_query}' не дал результата. "
                    "Честно скажи пользователю, что найти не удалось, и предложи что-то другое. "
                    "Новых инструментов не запускай."
                )
                decision = await process_user_input(user_text, state_manager, memory_context=empty_search)

        elif decision.get("memory_query_topic"):
            memory_topic = decision["memory_query_topic"]
            ack_replies = _flatten_replies(decision.get("replies"))
            if ack_replies:
                ack_text = ack_replies[0]
                await update.message.reply_text(ack_text)
                await state_manager.add_history("model", ack_text)
            decision = await retrieve_memory(user_text, memory_topic, state_manager)

        elif decision.get("add_alarm"):
            alarm_data = decision["add_alarm"]
            ack_replies = _flatten_replies(decision.get("replies"))
            if ack_replies:
                ack_text = ack_replies[0]
                await update.message.reply_text(ack_text)
                await state_manager.add_history("model", ack_text)
            logger.info(f"⏰ [ALARM] Действие: {alarm_data}")
            alarm_context, alarm_record, ok = await _handle_alarm_action(alarm_data, state_manager)
            await state_manager.add_tool_record(alarm_record)
            if ok:
                followup = (
                    f"Инструмент будильника выполнен: {alarm_context} "
                    "Подтверди пользователю кратко, что сделано."
                )
            else:
                followup = (
                    f"⚠️ Инструмент будильника неуспешен: {alarm_context} "
                    "Честно скажи пользователю, что выполнить не удалось. Новых инструментов не запускай."
                )
            decision = await process_user_input(user_text, state_manager, memory_context=followup)

    return decision

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state_manager = context.bot_data["state_manager"]
    process_user_input = context.bot_data["process_user_input"]
    
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
        
        decision = await _tool_loop(
            update, context, state_manager, user_text, initial_decision
        )
        
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
        
        if interest_text := decision.get("add_interest"):
            if isinstance(interest_text, str) and interest_text.strip():
                await state_manager.add_interest(interest_text.strip())
        
        replies = _flatten_replies(decision.get("replies"))
        if not replies and (single_text := decision.get("text")):
            if isinstance(single_text, str) and len(single_text) > 0:
                logger.warning(f"⚠️ [JSON] Обнаружен ответ в поле 'text' вместо 'replies'. Исправляю.")
                replies = [single_text]
                
        if replies:
            for i, message_text in enumerate(replies):
                logger.info(f"💡<- {message_text}")
                await state_manager.add_history("model", message_text)
                asyncio.create_task(insert_to_rag(f"Бот: {message_text}", metadata=f"bot_{user_id}"))
                
                await update.message.reply_text(message_text)
                if i < len(replies) - 1:
                    await context.bot.send_chat_action(chat_id=user_id, action=ChatAction.TYPING)
                    next_len = len(replies[i+1])
                    await asyncio.sleep(min(1.0 + next_len * 0.06, 3.0))
        else:
            logger.info("💡<- [молчание]")
    finally:
        stop_typing.set()

async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state_manager = context.bot_data["state_manager"]
    process_user_input = context.bot_data["process_user_input"]
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
        
        decision = await _tool_loop(
            update, context, state_manager, text, initial_decision
        )
        
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
        
        replies = _flatten_replies(decision.get("replies"))
        if replies:
            for i, message_text in enumerate(replies):
                logger.info(f"💡<- {message_text}")
                await state_manager.add_history("model", message_text)
                asyncio.create_task(insert_to_rag(f"Бот: {message_text}", metadata=f"bot_{user_id}"))
                await update.message.reply_text(message_text)
                if i < len(replies) - 1:
                    await context.bot.send_chat_action(chat_id=user_id, action=ChatAction.TYPING)
                    next_len = len(replies[i+1])
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

            replies = _flatten_replies(decision.get("replies"))
            if not replies and (single_text := decision.get("text")):
                if isinstance(single_text, str) and len(single_text) > 0:
                    logger.warning(f"⚠️ [JSON] Обнаружен ответ в поле 'text' вместо 'replies'. Исправляю.")
                    replies = [single_text]

            if replies:
                for text_to_send in replies:
                    if text_to_send:
                        await context.bot.send_message(chat_id=config.ALLOWED_USER_ID, text=text_to_send)
                        await state_manager.add_history("model", text_to_send)
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
                        await context.bot.send_message(chat_id=config.ALLOWED_USER_ID, text=text_to_send)
                        await state_manager.add_history("model", text_to_send)
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