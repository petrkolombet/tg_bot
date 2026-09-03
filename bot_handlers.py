# --- START OF FILE bot_handlers.py ---

import logging
import asyncio
import random
import datetime
import os
import json
import subprocess
from datetime import timezone
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatAction
from telegram.ext import ContextTypes

import config
import providers
import server_access
import tools_registry
from bot_ai import summarize_tool_output
from rag import vector_search

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

# Дедуп сообщений: chat_id -> текст последнего ОТПРАВЛЕННОГО сообщения.
# Если новое сообщение дословно совпадает с последним — не шлём (модель
# склонна повторять собственные фразы из истории).
_last_sent_text: dict = {}


async def _send_md(update_or_ctx, chat_id, text, reply_to_message_id=None):
    """Отправка с рендером Markdown + дедуп. Если текст дословно равен последнему
    отправленному в этот чат — пропускаем (False). Если Markdown невалиден
    (Telegram бросит Bad Request) — отправляем чистым текстом без parse_mode."""
    if text and text == _last_sent_text.get(chat_id):
        logger.info(f"⏭️ [DEDUP] Пропускаю дубль сообщения: {text[:80]}")
        return False
    try:
        await update_or_ctx.bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode="Markdown",
            reply_to_message_id=reply_to_message_id,
        )
    except Exception:
        logger.warning("⚠️ [SEND] Markdown битый, отправляю plain text")
        await update_or_ctx.bot.send_message(
            chat_id=chat_id,
            text=text,
            reply_to_message_id=reply_to_message_id,
        )
    _last_sent_text[chat_id] = text
    logger.info(f"💡<- {text}")
    return True


async def _send_acks(update, context, state_manager, ack_replies, reply_to_message_id=None):
    """Отправка ВСЕХ ack-реплик, пришедших вместе с tool_call/командой/поиском.

    Поведение как у финального ответа в handle_message:
      - каждая реплика проходит дедуп (дословный повтор последнего не шлём);
      - реально отправленное логируется '💡<-' и пишется в историю (никаких
        фантомов в истории и логах);
      - между репликами пауза с типингом, как в обычном ответе.
    Возвращает количество отправленных сообщений."""
    chat_id = update.effective_user.id
    reply_id = reply_to_message_id or getattr(update.message, "message_id", None)
    flat = _flatten_replies(ack_replies)
    sent_n = 0
    for i, ack_text in enumerate(flat):
        if not ack_text:
            continue
        sent = await _send_md(context, chat_id, ack_text, reply_to_message_id=reply_id)
        if not sent:
            continue
        await state_manager.add_history("model", ack_text)
        sent_n += 1
        if i < len(flat) - 1:
            await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
            await asyncio.sleep(min(1.0 + len(flat[i + 1]) * 0.06, 3.0))
    return sent_n

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
                 or d.get("add_alarm") or d.get("tool_call"))
        )

    def tool_key(d):
        if d.get("tool_call"):
            return ("tool", d["tool_call"])
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
                "Новых инструментов не запускай. НЕ ДУБЛИРУЙ: не повторяй дословно сообщения, "
                "которые уже видишь в истории. Напиши новый ответ."
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

        if decision.get("tool_call"):
            call_str = decision["tool_call"]
            try:
                tool_name, method, kwargs = tools_registry.parse_tool_call(call_str)
            except ValueError as e:
                logger.warning(f"⚠️ [TOOLS] Плохой tool_call: {e}")
                bad = (
                    f"⚠️ tool_call '{call_str}' не распознан: {e}. "
                    "Проверь имя инструмента, метод и обязательные аргументы по каталогу. "
                    "Либо ответь пользователю текстом, либо исправь вызов."
                )
                decision = await process_user_input(user_text, state_manager, memory_context=bad)
                continue

            ack_replies = _flatten_replies(decision.get("replies"))
            if ack_replies:
                await _send_acks(update, context, state_manager, ack_replies)

            # Встроенные тулы → диспатч на существующие поля и повторный проход цикла
            if tool_name in tools_registry.BUILTIN_NAMES:
                if tool_name == "shell" and method == "run":
                    decision = {"server_command": {"cmd": kwargs.get("cmd", ""), "desc": kwargs.get("desc", "")}}
                elif tool_name == "search" and method == "run":
                    decision = {"google_search": kwargs.get("query", "")}
                elif tool_name == "memory" and method == "query":
                    decision = {"memory_query_topic": kwargs.get("topic", "")}
                else:
                    logger.warning(f"⚠️ [TOOLS] Неизвестный встроенный метод {tool_name}.{method}")
                    decision = await process_user_input(
                        user_text, state_manager,
                        memory_context=f"⚠️ Инструмент '{tool_name}.{method}' неизвестен. Используй методы из каталога.",
                    )
                continue

            # Кастомный тул из workspace/tools
            logger.info(f"🔧 [TOOL] {call_str}")
            manifest = tools_registry.get_tool_manifest(tool_name)
            script_path = manifest.get("file")
            method_schema = manifest["methods"][method]
            result = await server_access.execute_custom_tool(script_path, method, kwargs=kwargs, timeout=60)
            output = result.get("output", "")
            error = result.get("error", "")

            if result.get("allowed", True) is False:
                out_block = error or "🚫 Выполнение тула запрещено"
            else:
                out_block = output

            logger.info(f"✅🔧 [RESULT] {out_block}")

            # Если file.read прочитал картинку — тул вернул {kind:"image", absolute_path,...}.
            # Тогда прикрепляем картинку к следующему g4f-запросу (полный промт + image_url),
            # как если бы пользователь прислал её в чат.
            image_path = None
            try:
                tool_raw = json.loads(out_block) if out_block.strip().startswith("{") else {}
            except (ValueError, TypeError):
                tool_raw = {}
            tool_result = tool_raw.get("result", tool_raw) if isinstance(tool_raw, dict) else {}
            if isinstance(tool_result, dict) and tool_result.get("kind") == "image":
                image_path = tool_result.get("absolute_path")
                out_block = f"Изображение {tool_result.get('path', '?')} ({tool_result.get('size', '?')} байт) прикреплено к запросу."

            if len(out_block) <= config.TOOL_RESULT_LIMIT:
                report = out_block
            else:
                report = await summarize_tool_output(call_str, method_schema.get("description", ""), out_block, state_manager)
            rec = f'🔧 вызвал: {call_str} → {report}'
            await state_manager.add_tool_record(rec)

            tool_context = f"Результат вызова тула '{call_str}':\n---\n{out_block}\n---"
            if error:
                tool_context += f"\nОшибки stderr:\n{error}"
            decision = await process_user_input(user_text, state_manager, memory_context=tool_context, image_path=image_path)

        elif decision.get("server_command"):
            server_spec = decision["server_command"]
            server_cmd = server_spec.get("cmd") if isinstance(server_spec, dict) else server_spec
            server_desc = server_spec.get("desc", "") if isinstance(server_spec, dict) else ""

            ack_replies = _flatten_replies(decision.get("replies"))
            if ack_replies:
                await _send_acks(update, context, state_manager, ack_replies)

            logger.info(f"🖥️ [SERVER] Команда: {server_cmd} ({server_desc})")
            result = await server_access.execute(server_cmd, timeout=30)
            output = result.get("output", "")
            error = result.get("error", "")
            server_context = f"Результат выполнения команды '{server_cmd}':\n---\n{output}\n---"
            if error:
                server_context += f"\nОшибки:\n{error}"

            if len(output) <= config.TOOL_RESULT_LIMIT:
                report = output
            else:
                report = await summarize_tool_output(server_cmd, server_desc, output, state_manager)
            logger.info(f"🖥️ [SERVER] ✅ {server_cmd} → {report}")
            rec = f'⚙️ выполнил: {server_cmd} → {report}'
            await state_manager.add_tool_record(rec)

            decision = await process_user_input(
                user_text, state_manager, memory_context=server_context
            )

        elif decision.get("google_search"):
            search_query = decision["google_search"]
            ack_replies = _flatten_replies(decision.get("replies"))
            if ack_replies:
                await _send_acks(update, context, state_manager, ack_replies)
            logger.info(f"🔍 [SEARCH] Запрос к поиску: {search_query}")
            search_result = await search_web(search_query)
            if search_result:
                # В память — результат поиска, жадно обрезанный до лимита (без DeepSeek-выжимки)
                search_report = search_result[:config.TOOL_RESULT_LIMIT]
                if len(search_result) > config.TOOL_RESULT_LIMIT:
                    search_report += f"\n... (всего {len(search_result)} симв.)"
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
                await _send_acks(update, context, state_manager, ack_replies)
            decision = await retrieve_memory(user_text, memory_topic, state_manager)

        elif decision.get("add_alarm"):
            alarm_data = decision["add_alarm"]
            ack_replies = _flatten_replies(decision.get("replies"))
            if ack_replies:
                await _send_acks(update, context, state_manager, ack_replies)
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

    # Если редактируем переменную .env — перехватываем ввод
    editing_key = context.user_data.get("editing_env_key")
    if editing_key:
        context.user_data.pop("editing_env_key", None)

        if editing_key.startswith("fb_"):
            # Фолбек: формат "fb_section"
            section = editing_key[3:]
            if section not in FALLBACK_KEYS:
                await update.message.reply_text("❌ неизвестная секция")
                return
            fb_url_key, fb_key_key, fb_model_key = FALLBACK_KEYS[section]
            parts = user_text.strip().split()
            prov_name = context.user_data.pop("editing_provider", None)

            if prov_name and prov_name in PROVIDERS:
                prov_url, prov_key, _, _ = PROVIDERS[prov_name]
                updates = {fb_url_key: prov_url, fb_model_key: parts[0]}
                if len(parts) == 2:
                    updates[fb_key_key] = parts[1]
                elif fb_key_key and prov_key:
                    updates[fb_key_key] = prov_key
                _write_env(updates)
                text = f"✅ Фолбек {prov_name.upper()}: `{parts[0]}`"
                if len(parts) == 2:
                    text += f"\nКлюч: `{parts[1]}`"
            else:
                if len(parts) == 3:
                    _write_env({fb_url_key: parts[0], fb_model_key: parts[1], fb_key_key: parts[2]})
                    text = f"✅ Фолбек URL: `{parts[0]}`\nМодель: `{parts[1]}`\nКлюч: `{parts[2]}`"
                elif len(parts) == 2:
                    if parts[1].startswith("sk-") or parts[1].startswith("key-"):
                        _write_env({fb_model_key: parts[0], fb_key_key: parts[1]})
                        text = f"✅ Фолбек Модель: `{parts[0]}`\nКлюч: `{parts[1]}`"
                    else:
                        _write_env({fb_url_key: parts[0], fb_model_key: parts[1]})
                        text = f"✅ Фолбек URL: `{parts[0]}`\nМодель: `{parts[1]}`"
                else:
                    model = parts[0]
                    found = False
                    for pn, (pu, pk, pm, pp) in PROVIDERS.items():
                        if model == pm:
                            _write_env({fb_url_key: pu, fb_model_key: model})
                            if fb_key_key and pk:
                                _write_env({fb_key_key: pk})
                            text = f"✅ Фолбек {pn.upper()}\nURL: `{pu}`\nМодель: `{model}`"
                            found = True
                            break
                    if not found:
                        _write_env({fb_model_key: model})
                        text = f"✅ Фолбек Модель: `{model}`"

            await update.message.reply_text(f"{text}\n\nперезапускаю...", parse_mode="Markdown")

        elif editing_key in LLM_SECTIONS:
            # Формат: "модель" или "модель ключ"
            parts = user_text.strip().split()
            url_key, key_key, model_key = SECTION_KEYS[editing_key]
            prov_name = context.user_data.pop("editing_provider", None)

            if prov_name and prov_name in PROVIDERS:
                prov_url, prov_key, _, _ = PROVIDERS[prov_name]
                updates = {url_key: prov_url, model_key: parts[0]}
                if len(parts) == 2:
                    updates[key_key] = parts[1]
                elif key_key and prov_key:
                    updates[key_key] = prov_key
                _write_env(updates)
                text = f"✅ {prov_name.upper()}: `{parts[0]}`"
                if len(parts) == 2:
                    text += f"\nКлюч: `{parts[1]}`"
            else:
                if len(parts) == 3:
                    _write_env({url_key: parts[0], model_key: parts[1], key_key: parts[2]})
                    text = f"✅ URL: `{parts[0]}`\nМодель: `{parts[1]}`\nКлюч: `{parts[2]}`"
                elif len(parts) == 2:
                    if parts[1].startswith("sk-") or parts[1].startswith("key-"):
                        _write_env({model_key: parts[0], key_key: parts[1]})
                        text = f"✅ Модель: `{parts[0]}`\nКлюч: `{parts[1]}`"
                    else:
                        _write_env({url_key: parts[0], model_key: parts[1]})
                        text = f"✅ URL: `{parts[0]}`\nМодель: `{parts[1]}`"
                else:
                    model = parts[0]
                    found = False
                    for pn, (pu, pk, pm, pp) in PROVIDERS.items():
                        if model == pm:
                            _write_env({url_key: pu, model_key: model})
                            if key_key and pk:
                                _write_env({key_key: pk})
                            text = f"✅ {pn.upper()}\nURL: `{pu}`\nМодель: `{model}`"
                            found = True
                            break
                    if not found:
                        _write_env({model_key: model})
                        text = f"✅ Модель: `{model}`"

            await update.message.reply_text(f"{text}\n\nперезапускаю...", parse_mode="Markdown")
        else:
            # Старый формат: одиночная переменная
            _write_env({editing_key: user_text.strip()})
            await update.message.reply_text(f"✅ `{editing_key}` = `{user_text.strip()}`\n\nперезапускаю...", parse_mode="Markdown")

        import subprocess
        subprocess.Popen(
            ["systemctl", "restart", "tg-bot.service"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        return

    # Если редактируем прокси провайдера — перехватываем ввод
    editing_proxy = context.user_data.get("editing_proxy")
    if editing_proxy:
        context.user_data.pop("editing_proxy", None)
        proxy_env_key = PROXY_ENV_KEYS.get(editing_proxy, "")
        if not proxy_env_key:
            await update.message.reply_text("❌ неизвестный провайдер")
            return

        proxy_str = user_text.strip()
        # Валидация формата host:port:user:pass
        parts = proxy_str.split(":")
        if len(parts) not in (2, 4):
            await update.message.reply_text(
                "❌ неверный формат. Используй:\n"
                "`host:port:user:pass`\n"
                "или `host:port` (без аутентификации)",
                parse_mode="Markdown",
            )
            return

        # Сохраняем в .env
        _write_env({proxy_env_key: proxy_str})

        # Сохраняем в saved_proxies.json если ещё нет
        try:
            with open("/root/tg_bot/saved_proxies.json", "r") as f:
                saved = json.load(f)
        except Exception:
            saved = []

        host = parts[0]
        # Проверяем есть ли уже такой прокси
        exists = any(sp['host'] == host for sp in saved)
        if not exists and len(parts) == 4:
            saved.append({
                "name": f"Кастомный ({host})",
                "host": parts[0],
                "port": parts[1],
                "user": parts[2],
                "pass": parts[3],
            })
            with open("/root/tg_bot/saved_proxies.json", "w") as f:
                json.dump(saved, f, indent=2, ensure_ascii=False)

        await update.message.reply_text(
            f"✅ Прокси для {editing_proxy.upper()}: `{proxy_str}`\n\nперезапускаю...",
            parse_mode="Markdown",
        )
        import subprocess
        subprocess.Popen(
            ["systemctl", "restart", "tg-bot.service"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        return

    stop_typing = asyncio.Event()
    typing_task = asyncio.create_task(_typing_loop(context.bot, user_id, stop_typing))

    epoch = context.bot_data.get("gen_epoch", 0) + 1
    context.bot_data["gen_epoch"] = epoch
    context.bot_data["gen_task"] = asyncio.current_task()

    try:
        await state_manager.check_and_apply_peak_decay()
        await state_manager.add_history("user", user_text)
        await state_manager.update_interaction()

        # Новый слой: векторная память по смыслу сообщения (подмешиваем, если есть совпадения)
        hits = await vector_search(user_text)
        if hits:
            block = "\n".join(f"- {h}" for h in hits)
            state_manager.state["active_memory"] = f"Вот факты из памяти, которые относятся к разговору. Используй их, только если они нужны прямо сейчас, не упоминай если не имеют отношения:\n{block}"
            logger.info(f"🧠 [VECTOR+] Подмешал {len(hits)} факт(ов) в контекст: {' | '.join(h[:40] for h in hits)}")
        else:
            state_manager.state.pop("active_memory", None)
            logger.info("🧠 [VECTOR-] Поискал, релевантных фактов нет — контекст чистый")

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
            if epoch != context.bot_data.get("gen_epoch", 0):
                logger.info("🛑 генерация перебита (/stop или новое сообщение) — ответ не отправлен")
            else:
                sent_count = 0
                for i, message_text in enumerate(replies):
                    sent = await _send_md(context, user_id, message_text, reply_to_message_id=update.message.message_id)
                    if not sent:
                        continue
                    await state_manager.add_history("model", message_text)
                    sent_count += 1
                    if i < len(replies) - 1:
                        await context.bot.send_chat_action(chat_id=user_id, action=ChatAction.TYPING)
                        next_len = len(replies[i+1])
                        await asyncio.sleep(min(1.0 + next_len * 0.06, 3.0))
                if not sent_count:
                    logger.info("💡<- [все реплики продублированы дедупом]")
        else:
            logger.info("💡<- [молчание]")
    finally:
        stop_typing.set()
        if context.bot_data.get("gen_task") is asyncio.current_task():
            context.bot_data["gen_task"] = None

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
            sent_count = 0
            for i, message_text in enumerate(replies):
                sent = await _send_md(context, user_id, message_text, reply_to_message_id=update.message.message_id)
                if not sent:
                    continue
                await state_manager.add_history("model", message_text)
                sent_count += 1
                if i < len(replies) - 1:
                    await context.bot.send_chat_action(chat_id=user_id, action=ChatAction.TYPING)
                    next_len = len(replies[i+1])
                    await asyncio.sleep(min(1.0 + next_len * 0.06, 3.0))
            if not sent_count:
                logger.info("💡<- [все реплики продублированы дедупом]")
        else:
            logger.info("💡<- [молчание]")
    finally:
        stop_typing.set()

async def handle_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Приём и сохранение входящих файлов (документы, фото, аудио, видео).
    Файл сохраняется в INCOMING_DIR с оригинальным именем (или сгенерированным
    по типу), в историю уходит запись [файл]: путь. Голосовые/кружки НЕ трогает —
    у них отдельный хендлер."""
    if not update.message or update.effective_user.id != config.ALLOWED_USER_ID:
        return

    msg = update.message
    user_id = update.effective_user.id
    state_manager = context.bot_data["state_manager"]
    process_user_input = context.bot_data["process_user_input"]

    # Определяем источник, оригинальное имя и тип
    src = None
    orig_name = None
    if msg.document:
        src = msg.document
        orig_name = msg.document.file_name
    elif msg.photo:
        src = msg.photo[-1]  # самое большое разрешение
        orig_name = None
    elif msg.audio:
        src = msg.audio
        orig_name = getattr(msg.audio, "file_name", None)
    elif msg.video:
        src = msg.video
        orig_name = getattr(msg.video, "file_name", None)
    else:
        return

    if not src:
        return

    kind = "photo" if msg.photo else ("video" if msg.video else ("audio" if msg.audio else "doc"))
    ext = os.path.splitext(orig_name)[1] if orig_name else ""
    if not ext:
        ext = {kind: ".jpg" if kind == "photo" else ".mp4" if kind == "video" else ".mp3" if kind == "audio" else ".bin"}[kind]

    # Уникальное имя: оригинальное, при коллизии — с таймштампом
    os.makedirs(config.INCOMING_DIR, exist_ok=True)
    base = os.path.splitext(orig_name)[0] if orig_name else kind
    candidate = os.path.join(config.INCOMING_DIR, (orig_name or f"{base}{ext}"))
    if os.path.exists(candidate):
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        candidate = os.path.join(config.INCOMING_DIR, f"{base}_{ts}{ext}")
    # если и так коллизия (одна и та же секунда) — добиваем счётчиком
    n = 2
    while os.path.exists(candidate):
        candidate = os.path.join(config.INCOMING_DIR, f"{os.path.splitext(candidate)[0]}_{n}{ext}")

    stop_typing = asyncio.Event()
    typing_task = asyncio.create_task(_typing_loop(context.bot, user_id, stop_typing))
    try:
        file = None
        for attempt in range(3):
            try:
                file = await context.bot.get_file(src.file_id)
                break
            except Exception as e:
                logger.warning(f"⚠️ [FILE] get_file попытка {attempt+1}: {e}")
                if attempt < 2:
                    await asyncio.sleep(2)
        if not file:
            await update.message.reply_text("не скачал файл, повтори")
            return

        downloaded = await file.download_as_bytearray()
        with open(candidate, "wb") as f:
            f.write(downloaded)

        logger.info(f"📄-> Файл сохранён: {candidate} ({len(downloaded)} байт)")
        await state_manager.add_history("user", f"[файл]: {candidate}")
        await state_manager.update_interaction()

        # Пропускаем через обычный конвейер, чтобы модель "знала" о файле
        initial_decision = await process_user_input(f"[файл]: {candidate}", state_manager)
        decision = await _tool_loop(update, context, state_manager, f"[файл]: {candidate}", initial_decision)

        if not decision:
            return

        replies = _flatten_replies(decision.get("replies"))
        if not replies and (single_text := decision.get("text")):
            replies = [single_text] if isinstance(single_text, str) and len(single_text) > 0 else []
        for i, message_text in enumerate(replies):
            sent = await _send_md(context, user_id, message_text, reply_to_message_id=update.message.message_id)
            if not sent:
                continue
            await state_manager.add_history("model", message_text)
            if i < len(replies) - 1:
                await context.bot.send_chat_action(chat_id=user_id, action=ChatAction.TYPING)
                await asyncio.sleep(1.0)
    except Exception as e:
        logger.error(f"💥 [FILE] Ошибка сохранения: {e}", exc_info=True)
        await update.message.reply_text(f"❌ ошибка при сохранении файла: {e}")
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
        if state_manager.state.get("messages_since_summary", 0) >= config.CHAT_HISTORY_LIMIT:
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
                        await _send_md(context, config.ALLOWED_USER_ID, text_to_send)
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
                        await _send_md(context, config.ALLOWED_USER_ID, text_to_send)
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

async def handle_sum(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ручной запуск обновления саммари: /sum"""
    if not update.message or update.effective_user.id != config.ALLOWED_USER_ID:
        return
    state_manager = context.bot_data["state_manager"]
    update_longterm_summary = context.bot_data["update_longterm_summary"]
    await update.message.reply_text("🔄 обновляю саммари...")
    try:
        await update_longterm_summary(state_manager)
        summary = state_manager.state.get("summary", "(пусто)")
        await update.message.reply_text(f"✅ саммари обновлено:\n{summary}")
    except Exception as e:
        logger.error("💥 [SUM] Ошибка обновления саммари!", exc_info=True)
        await update.message.reply_text(f"❌ ошибка: {e}")

async def handle_think(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ручной запуск генерации мыслей (рефлексии): /think"""
    if not update.message or update.effective_user.id != config.ALLOWED_USER_ID:
        return
    state_manager = context.bot_data["state_manager"]
    generate_reflection = context.bot_data["generate_reflection"]
    await update.message.reply_text("💭 запускаю рефлексию...")
    try:
        thoughts = await generate_reflection(state_manager)
        if not thoughts:
            await update.message.reply_text("💭 рефлексия не дала новых мыслей (мало истории или уже актуально)")
            return
        await state_manager.add_thoughts(thoughts)
        text = "💭 новые мысли:\n" + "\n".join(f"- {t}" for t in thoughts)
        await update.message.reply_text(text)
    except Exception as e:
        logger.error("💥 [THINK] Ошибка рефлексии!", exc_info=True)
        await update.message.reply_text(f"❌ ошибка: {e}")


# --- /stop: остановка текущей генерации ---

async def handle_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Останавливает текущую генерацию ответа."""
    if not update.message or update.effective_user.id != config.ALLOWED_USER_ID:
        return
    import bot_ai
    bot_ai.request_stop()
    # Перебиваем любую генерацию, идущую в этот момент
    if "gen_epoch" in context.bot_data:
        context.bot_data["gen_epoch"] = context.bot_data.get("gen_epoch", 0) + 1
    task = context.bot_data.get("gen_task")
    if task is not None and task is not asyncio.current_task() and not task.done():
        task.cancel()
    await update.message.reply_text("⏹ остановлено")


# --- /restart: перезапуск бота ---

async def handle_restart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Перезапускает tg-bot.service через systemctl."""
    if not update.message or update.effective_user.id != config.ALLOWED_USER_ID:
        return
    await update.message.reply_text("🔄 перезапускаю бота...")
    try:
        subprocess.Popen(
            ["systemctl", "restart", "tg-bot.service"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception as e:
        await update.message.reply_text(f"❌ ошибка перезапуска: {e}")


# --- /models: выбор модели ---

def _read_env():
    """Читает .env в dict."""
    env = {}
    try:
        with open(".env", "r") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return env

def _write_env(updates: dict):
    """Обновляет переменные в .env (перезаписывает файл)."""
    lines = []
    env = _read_env()
    env.update(updates)
    # Перезаписываем в том же порядке, добавляем новые в конец
    written = set()
    try:
        with open(".env", "r") as f:
            for line in f:
                stripped = line.strip()
                if stripped and not stripped.startswith("#") and "=" in stripped:
                    k = stripped.split("=", 1)[0].strip()
                    if k in env:
                        lines.append(f'{k}="{env[k]}"')
                        written.add(k)
                        continue
                lines.append(line.rstrip())
    except FileNotFoundError:
        pass
    # Добавляем новые переменные
    for k, v in env.items():
        if k not in written:
            lines.append(f'{k}="{v}"')
    with open(".env", "w") as f:
        f.write("\n".join(lines) + "\n")

# Секции моделей: ключ -> (название, [(config_key, label), ...])
LLM_SECTIONS = {
    "main": ("🧠 Основная генерация", [
        ("G4F_URL", "URL"),
        ("G4F_KEY", "Ключ"),
        ("G4F_MODEL", "Модель"),
        ("MAIN_FALLBACK_URL", "Фолбек URL"),
        ("MAIN_FALLBACK_KEY", "Фолбек Ключ"),
        ("MAIN_FALLBACK_MODEL", "Фолбек Модель"),
    ]),
    "search": ("🔍 Поиск", [
        ("SEARCH_PROXY_URL", "URL"),
        ("SEARCH_PROXY_KEY", "Ключ"),
        ("SEARCH_MODEL", "Модель"),
        ("SEARCH_FALLBACK_URL", "Фолбек URL"),
        ("SEARCH_FALLBACK_KEY", "Фолбек Ключ"),
        ("SEARCH_FALLBACK_MODEL", "Фолбек Модель"),
    ]),
    "summary": ("📝 Саммари", [
        ("SUMMARY_PROXY_URL", "URL"),
        ("SUMMARY_PROXY_KEY", "Ключ"),
        ("SUMMARY_MODEL", "Модель"),
        ("SUMMARY_FALLBACK_URL", "Фолбек URL"),
        ("SUMMARY_FALLBACK_KEY", "Фолбек Ключ"),
        ("SUMMARY_FALLBACK_MODEL", "Фолбек Модель"),
    ]),
    "reflection": ("💭 Рефлексия", [
        ("REFLECTION_PROXY_URL", "URL"),
        ("REFLECTION_PROXY_KEY", "Ключ"),
        ("REFLECTION_MODEL", "Модель"),
        ("REFLECTION_FALLBACK_URL", "Фолбек URL"),
        ("REFLECTION_FALLBACK_KEY", "Фолбек Ключ"),
        ("REFLECTION_FALLBACK_MODEL", "Фолбек Модель"),
    ]),
    "rag": ("🧠 RAG (память)", [
        ("RAG_URL", "URL"),
        ("RAG_KEY", "Ключ"),
        ("RAG_MODEL", "Модель"),
        ("RAG_FALLBACK_URL", "Фолбек URL"),
        ("RAG_FALLBACK_KEY", "Фолбек Ключ"),
        ("RAG_FALLBACK_MODEL", "Фолбек Модель"),
    ]),
    "embed": ("🔢 Эмбеддинги (память)", [
        ("EMBED_URL", "URL"),
        ("EMBED_KEY", "Ключ"),
        ("EMBED_MODEL", "Модель"),
        ("EMBED_FALLBACK_URL", "Фолбек URL"),
        ("EMBED_FALLBACK_KEY", "Фолбек Ключ"),
        ("EMBED_FALLBACK_MODEL", "Фолбек Модель"),
    ]),
}

# Провайдеры, модели, секции и фолбеки — ЕДИНСТВЕННЫЙ источник: providers.py
from providers import (
    PROVIDERS,
    PROVIDER_MODELS,
    OPENROUTER_MODEL_MAP,
    SECTION_KEYS,
    FALLBACK_KEYS,
    PROXY_ENV_KEYS,
)

async def handle_models(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает текущие настройки LLM и кнопки для редактирования."""
    if not update.message or update.effective_user.id != config.ALLOWED_USER_ID:
        return

    buttons = []
    for key, (name, _) in LLM_SECTIONS.items():
        buttons.append([InlineKeyboardButton(name, callback_data=f"llm:{key}")])

    markup = InlineKeyboardMarkup(buttons)
    text = "⚙️ настройки LLM\n\nвыбери секцию для настройки:"
    await update.message.reply_text(text, reply_markup=markup)


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка нажатий на InlineKeyboard."""
    query = update.callback_query
    if not query or update.effective_user.id != config.ALLOWED_USER_ID:
        return

    data = query.data
    await query.answer()

    if data.startswith("llm:"):
        section = data.split(":", 1)[1]
        if section not in LLM_SECTIONS:
            return

        section_name, fields = LLM_SECTIONS[section]
        url_key, key_key, model_key = SECTION_KEYS[section]
        fb_url_key, fb_key_key, fb_model_key = FALLBACK_KEYS[section]

        # Текущие значения
        current_url = getattr(providers, url_key, "—")
        current_model = getattr(providers, model_key, "—")
        current_fb_url = getattr(providers, fb_url_key, "")
        current_fb_model = getattr(providers, fb_model_key, "")

        # Определяем текущий провайдер
        current_provider = "custom"
        for prov_name, (prov_url, prov_key, prov_model, _) in PROVIDERS.items():
            if current_url == prov_url and current_model == prov_model:
                current_provider = prov_name
                break

        current_fb_provider = "не настроен"
        if current_fb_url:
            for prov_name, (prov_url, prov_key, prov_model, _) in PROVIDERS.items():
                if current_fb_url == prov_url and current_fb_model == prov_model:
                    current_fb_provider = prov_name
                    break
            else:
                current_fb_provider = "custom"

        text = f"{section_name}\n\n"
        text += f"**Основной:** `{current_provider.upper()}`\n"
        text += f"URL: `{current_url}`\n"
        text += f"Модель: `{current_model}`\n\n"
        text += f"**Фолбек:** `{current_fb_provider}`\n"
        if current_fb_url:
            text += f"URL: `{current_fb_url}`\n"
            text += f"Модель: `{current_fb_model}`\n"

        buttons = [
            [InlineKeyboardButton("⚡ Основной провайдер", callback_data=f"llm_main:{section}")],
            [InlineKeyboardButton("🔁 Фолбек провайдер", callback_data=f"llm_fb:{section}")],
            [InlineKeyboardButton("⬅️ назад", callback_data="llm_back")],
        ]
        markup = InlineKeyboardMarkup(buttons)
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=markup)

    elif data.startswith("llm_main:"):
        section = data.split(":", 1)[1]
        url_key, key_key, model_key = SECTION_KEYS[section]
        current_url = getattr(providers, url_key, "—")
        current_model = getattr(providers, model_key, "—")

        current_provider = "custom"
        for prov_name, (prov_url, prov_key, prov_model, _) in PROVIDERS.items():
            if current_url == prov_url and current_model == prov_model:
                current_provider = prov_name
                break

        text = f"{LLM_SECTIONS[section][0]} — основной\n\n"
        text += f"URL: `{current_url}`\n"
        text += f"Модель: `{current_model}`\n\n"
        text += "выбери провайдер:\n"

        buttons = []
        for prov_name, (prov_url, prov_key, prov_model, _) in PROVIDERS.items():
            label = prov_name.upper()
            if prov_name == current_provider:
                label = f"✅ {label}"
            buttons.append([InlineKeyboardButton(label, callback_data=f"prov:{section}:{prov_name}")])

        buttons.append([InlineKeyboardButton("✏️ Свой провайдер", callback_data=f"custom:{section}")])
        buttons.append([InlineKeyboardButton("⬅️ назад", callback_data=f"llm:{section}")])
        markup = InlineKeyboardMarkup(buttons)
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=markup)

    elif data.startswith("llm_fb:"):
        section = data.split(":", 1)[1]
        fb_url_key, fb_key_key, fb_model_key = FALLBACK_KEYS[section]
        current_fb_url = getattr(providers, fb_url_key, "")
        current_fb_model = getattr(providers, fb_model_key, "")

        current_fb_provider = "не настроен"
        if current_fb_url:
            for prov_name, (prov_url, prov_key, prov_model, _) in PROVIDERS.items():
                if current_fb_url == prov_url and current_fb_model == prov_model:
                    current_fb_provider = prov_name
                    break
            else:
                current_fb_provider = "custom"

        text = f"{LLM_SECTIONS[section][0]} — фолбек\n\n"
        if current_fb_url:
            text += f"URL: `{current_fb_url}`\n"
            text += f"Модель: `{current_fb_model}`\n\n"
        else:
            text += "не настроен\n\n"
        text += "выбери провайдер:\n"

        buttons = []
        for prov_name, (prov_url, prov_key, prov_model, _) in PROVIDERS.items():
            label = prov_name.upper()
            if prov_name == current_fb_provider:
                label = f"✅ {label}"
            buttons.append([InlineKeyboardButton(label, callback_data=f"fbprov:{section}:{prov_name}")])

        buttons.append([InlineKeyboardButton("✏️ Свой провайдер", callback_data=f"fbcustom:{section}")])
        if current_fb_url:
            buttons.append([InlineKeyboardButton("🚫 Убрать фолбек", callback_data=f"fbdel:{section}")])
        buttons.append([InlineKeyboardButton("⬅️ назад", callback_data=f"llm:{section}")])
        markup = InlineKeyboardMarkup(buttons)
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=markup)

    elif data.startswith("custom:"):
        section = data.split(":", 1)[1]
        url_key, key_key, model_key = SECTION_KEYS[section]
        await query.edit_message_text(
            f"✏️ `{LLM_SECTIONS[section][0]}` — свой провайдер\n\n"
            f"напиши через пробел:\n"
            f"`URL модель ключ`\n\n"
            f"примеры:\n"
            f"`https://openrouter.ai/api/v1 google/gemini-2.5-flash sk-or-xxx`\n"
            f"`http://127.0.0.1:8000 yandex-alice sk-alice`\n"
            f"`gpt-4o` (только модель, URL останется прежним)",
            parse_mode="Markdown",
        )
        context.user_data["editing_env_key"] = section

    elif data.startswith("prov:"):
        parts = data.split(":")
        section = parts[1]
        prov_name = parts[2]
        if prov_name not in PROVIDERS:
            return

        prov_url, prov_key, prov_model, prov_proxy = PROVIDERS[prov_name]
        url_key, key_key, model_key = SECTION_KEYS[section]

        # Показываем модели провайдера
        models = PROVIDER_MODELS.get(prov_name, [])
        current_model = getattr(providers, model_key, "—")

        # Текущий прокси провайдера
        proxy_env_key = PROXY_ENV_KEYS.get(prov_name, "")
        current_proxy = getattr(providers, proxy_env_key, "") if proxy_env_key else ""
        proxy_status = f"`{current_proxy}`" if current_proxy else "нет"

        text = f"{LLM_SECTIONS[section][0]} — {prov_name.upper()}\n\n"
        text += f"URL: `{prov_url}`\n"
        text += f"Прокси: {proxy_status}\n\n"
        text += "выбери модель:\n"

        buttons = []
        # Всегда используем индексы для callback_data ( Telegram лимит 64 байта)
        for i, (model_id, model_name) in enumerate(models):
            label = model_name
            if model_id == current_model:
                label = f"✅ {label}"
            buttons.append([InlineKeyboardButton(label, callback_data=f"setmodel:{section}:{prov_name}:{i}")])

        buttons.append([InlineKeyboardButton("✏️ Своя модель", callback_data=f"custommodel:{section}:{prov_name}")])
        buttons.append([InlineKeyboardButton("🌐 Прокси", callback_data=f"proxy:{prov_name}")])
        buttons.append([InlineKeyboardButton("⬅️ назад", callback_data=f"llm:{section}")])
        markup = InlineKeyboardMarkup(buttons)
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=markup)

    elif data.startswith("setmodel:"):
        parts = data.split(":")
        section = parts[1]
        prov_name = parts[2]
        model_id = parts[3]
        prov_url, prov_key, _, _ = PROVIDERS[prov_name]
        url_key, key_key, model_key = SECTION_KEYS[section]

        # Разрешаем индекс в model_id для всех провайдеров
        if prov_name in PROVIDER_MODELS and model_id.isdigit():
            idx = int(model_id)
            models = PROVIDER_MODELS[prov_name]
            if 0 <= idx < len(models):
                model_id = models[idx][0]

        updates = {url_key: prov_url, model_key: model_id}
        # Ключ провайдера пишем в .env (у каждого свой)
        if key_key and prov_key:
            updates[key_key] = prov_key
        _write_env(updates)

        await query.edit_message_text(
            f"✅ {LLM_SECTIONS[section][0]}\n{prov_name.upper()}: {model_id}\n\nперезапускаю...",
        )
        import subprocess
        subprocess.Popen(
            ["systemctl", "restart", "tg-bot.service"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )

    elif data.startswith("custommodel:"):
        parts = data.split(":")
        section = parts[1]
        prov_name = parts[2]
        prov_url, prov_key, _, _ = PROVIDERS[prov_name]
        url_key, key_key, model_key = SECTION_KEYS[section]

        await query.edit_message_text(
            f"✏️ `{prov_name.upper()}` — своя модель\n\n"
            f"напиши модель и (опционально) ключ через пробел:\n"
            f"`gemini-2.5-flash`\n"
            f"`gpt-4o sk-or-xxx`",
            parse_mode="Markdown",
        )
        context.user_data["editing_env_key"] = section
        context.user_data["editing_provider"] = prov_name

    elif data.startswith("fbprov:"):
        parts = data.split(":")
        section = parts[1]
        prov_name = parts[2]
        if prov_name not in PROVIDERS:
            return

        prov_url, prov_key, prov_model, _prov_proxy = PROVIDERS[prov_name]
        fb_url_key, fb_key_key, fb_model_key = FALLBACK_KEYS[section]

        models = PROVIDER_MODELS.get(prov_name, [])
        current_fb_model = getattr(providers, fb_model_key, "")

        text = f"{LLM_SECTIONS[section][0]} — фолбек {prov_name.upper()}\n\n"
        text += f"URL: `{prov_url}`\n\n"
        text += "выбери модель:\n"

        buttons = []
        # Всегда используем индексы для callback_data (Telegram лимит 64 байта)
        for i, (model_id, model_name) in enumerate(models):
            label = model_name
            if model_id == current_fb_model:
                label = f"✅ {label}"
            buttons.append([InlineKeyboardButton(label, callback_data=f"fbsetmodel:{section}:{prov_name}:{i}")])

        buttons.append([InlineKeyboardButton("✏️ Своя модель", callback_data=f"fbcustommodel:{section}:{prov_name}")])
        buttons.append([InlineKeyboardButton("⬅️ назад", callback_data=f"llm_fb:{section}")])
        markup = InlineKeyboardMarkup(buttons)
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=markup)

    elif data.startswith("fbcustom:"):
        section = data.split(":", 1)[1]
        await query.edit_message_text(
            f"✏️ `{LLM_SECTIONS[section][0]}` — свой фолбек\n\n"
            f"напиши через пробел:\n"
            f"`URL модель ключ`\n\n"
            f"примеры:\n"
            f"`https://openrouter.ai/api/v1 google/gemini-2.5-flash sk-or-xxx`\n"
            f"`http://127.0.0.1:8000 yandex-alice sk-alice`\n"
            f"`gpt-4o` (только модель, URL останется прежним)",
            parse_mode="Markdown",
        )
        context.user_data["editing_env_key"] = f"fb_{section}"

    elif data.startswith("fbdel:"):
        section = data.split(":", 1)[1]
        fb_url_key, fb_key_key, fb_model_key = FALLBACK_KEYS[section]
        _write_env({fb_url_key: "", fb_key_key: "", fb_model_key: ""})
        await query.edit_message_text(
            f"🚫 Фолбек для {LLM_SECTIONS[section][0]} убран\n\nперезапускаю...",
        )
        import subprocess
        subprocess.Popen(
            ["systemctl", "restart", "tg-bot.service"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )

    elif data.startswith("fbsetmodel:"):
        parts = data.split(":")
        section = parts[1]
        prov_name = parts[2]
        model_id = parts[3]
        prov_url, prov_key, _, _ = PROVIDERS[prov_name]
        fb_url_key, fb_key_key, fb_model_key = FALLBACK_KEYS[section]

        # Разрешаем индекс в model_id для всех провайдеров
        if prov_name in PROVIDER_MODELS and model_id.isdigit():
            idx = int(model_id)
            models = PROVIDER_MODELS[prov_name]
            if 0 <= idx < len(models):
                model_id = models[idx][0]

        updates = {fb_url_key: prov_url, fb_model_key: model_id}
        # Ключ провайдера пишем в .env (у каждого свой)
        if fb_key_key and prov_key:
            updates[fb_key_key] = prov_key
        _write_env(updates)

        await query.edit_message_text(
            f"✅ {LLM_SECTIONS[section][0]} — фолбек\n{prov_name.upper()}: {model_id}\n\nперезапускаю...",
        )
        import subprocess
        subprocess.Popen(
            ["systemctl", "restart", "tg-bot.service"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )

    elif data.startswith("fbcustommodel:"):
        parts = data.split(":")
        section = parts[1]
        prov_name = parts[2]
        prov_url, prov_key, _, _ = PROVIDERS[prov_name]

        await query.edit_message_text(
            f"✏️ `{prov_name.upper()}` — своя модель (фолбек)\n\n"
            f"напиши модель и (опционально) ключ через пробел:\n"
            f"`gemini-2.5-flash`\n"
            f"`gpt-4o sk-or-xxx`",
            parse_mode="Markdown",
        )
        context.user_data["editing_env_key"] = f"fb_{section}"
        context.user_data["editing_provider"] = prov_name

    elif data.startswith("llm_edit:"):
        config_key = data.split(":", 1)[1]
        current = str(getattr(providers, config_key, ""))
        await query.edit_message_text(
            f"✏️ `{config_key}`\n\nтекущее: `{current}`\n\n"
            f"напиши новое значение:",
            parse_mode="Markdown",
        )
        context.user_data["editing_env_key"] = config_key

    elif data == "llm_back":
        buttons = []
        for key, (name, _) in LLM_SECTIONS.items():
            buttons.append([InlineKeyboardButton(name, callback_data=f"llm:{key}")])
        markup = InlineKeyboardMarkup(buttons)

        text = "⚙️ настройки LLM\n\n"
        for key, (name, _) in LLM_SECTIONS.items():
            url_key, _, model_key = SECTION_KEYS[key]
            fb_url_key, _, fb_model_key = FALLBACK_KEYS[key]

            current_url = getattr(providers, url_key, "")
            current_model = getattr(providers, model_key, "")
            current_fb_url = getattr(providers, fb_url_key, "")

            # Определяем имя провайдера
            provider_name = "custom"
            for pn, (pu, _, pm, __) in PROVIDERS.items():
                if current_url == pu and current_model == pm:
                    provider_name = pn
                    break

            fb_name = "выкл"
            if current_fb_url:
                fb_name = "custom"
                for pn, (pu, _, pm, __) in PROVIDERS.items():
                    if current_fb_url == pu and getattr(providers, fb_model_key, "") == pm:
                        fb_name = pn
                        break

            text += f"{name}\n"
            text += f"  ⚡ {provider_name.upper()}: `{current_model}`\n"
            text += f"  🔁 фолбек: {fb_name}\n"

        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=markup)

    # --- УПРАВЛЕНИЕ ПРОКСИ ---
    elif data.startswith("proxy:"):
        prov_name = data.split(":", 1)[1]
        proxy_env_key = PROXY_ENV_KEYS.get(prov_name, "")
        if not proxy_env_key:
            return
        current_proxy = getattr(providers, proxy_env_key, "")

        # Загружаем сохранённые прокси
        try:
            with open("/root/tg_bot/saved_proxies.json", "r") as f:
                saved = json.load(f)
        except Exception:
            saved = []

        text = f"🌐 Прокси для {prov_name.upper()}\n\n"
        text += f"текущий: `{current_proxy if current_proxy else 'нет'}`\n\n"
        text += "выбери или введи свой:\n"

        buttons = []
        buttons.append([InlineKeyboardButton("🚫 Без прокси", callback_data=f"proxyset:{prov_name}:none")])
        for i, sp in enumerate(saved):
            label = f"{sp['name']} ({sp['host']}:{sp['port']})"
            if current_proxy and sp['host'] in current_proxy:
                label = f"✅ {label}"
            buttons.append([InlineKeyboardButton(label, callback_data=f"proxyset:{prov_name}:saved:{i}")])
        buttons.append([InlineKeyboardButton("✏️ Свой прокси", callback_data=f"proxyset:{prov_name}:custom")])
        buttons.append([InlineKeyboardButton("⬅️ назад", callback_data=f"proxyback:{prov_name}")])
        markup = InlineKeyboardMarkup(buttons)
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=markup)

    elif data.startswith("proxyset:"):
        parts = data.split(":")
        prov_name = parts[1]
        mode = parts[2]
        proxy_env_key = PROXY_ENV_KEYS.get(prov_name, "")
        if not proxy_env_key:
            return

        # Сервисы которые нужно перезапускать при смене прокси
        PROXY_SERVICES = {
            "gpt": ["chatgpt-fallback"],
            "gemini": ["gemini-web2api"],
            "deepseek": ["freedeepseek-api"],
            "openrouter": ["tg-bot"],
            "g4f": ["tg-bot"],
            "alice": ["tg-bot"],
        }

        def _restart_services(prov):
            import subprocess
            services = PROXY_SERVICES.get(prov, ["tg-bot"])
            for svc in services:
                subprocess.Popen(
                    ["systemctl", "restart", f"{svc}.service"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )

        if mode == "none":
            _write_env({proxy_env_key: ""})
            await query.edit_message_text(
                f"🚫 Прокси для {prov_name.upper()} убран\n\nперезапускаю...",
            )
            _restart_services(prov_name)

        elif mode == "saved":
            idx = int(parts[3])
            try:
                with open("/root/tg_bot/saved_proxies.json", "r") as f:
                    saved = json.load(f)
                sp = saved[idx]
                proxy_str = f"{sp['host']}:{sp['port']}:{sp['user']}:{sp['pass']}"
                _write_env({proxy_env_key: proxy_str})
                await query.edit_message_text(
                    f"✅ Прокси для {prov_name.upper()}: `{sp['host']}:{sp['port']}`\n\nперезапускаю...",
                    parse_mode="Markdown",
                )
                _restart_services(prov_name)
            except Exception as e:
                await query.edit_message_text(f"❌ Ошибка: {e}")

        elif mode == "custom":
            await query.edit_message_text(
                f"✏️ `{prov_name.upper()}` — свой прокси\n\n"
                f"напиши в формате:\n"
                f"`host:port:user:pass`\n\n"
                f"пример:\n"
                f"`170.83.236.245:8000:15Uo6V:3HF2Fh`",
                parse_mode="Markdown",
            )
            context.user_data["editing_proxy"] = prov_name

    elif data.startswith("proxyback:"):
        prov_name = data.split(":", 1)[1]
        # Возвращаемся к списку провайдеров секции
        for section, (url_key, _, _) in SECTION_KEYS.items():
            url_val = getattr(providers, url_key, "")
            for pn, (pu, _, _, _) in PROVIDERS.items():
                if pn == prov_name and url_val == pu:
                    # Имитируем callback "prov:section:prov_name"
                    query._data = f"prov:{section}:{prov_name}"
                    await handle_callback(query, context)
                    return
        # Фолбек — на главную
        buttons = []
        for key, (name, _) in LLM_SECTIONS.items():
            buttons.append([InlineKeyboardButton(name, callback_data=f"llm:{key}")])
        markup = InlineKeyboardMarkup(buttons)
        await query.edit_message_text("⚙️ настройки LLM", reply_markup=markup)