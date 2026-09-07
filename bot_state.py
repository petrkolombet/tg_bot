# --- START OF FILE bot_state.py ---

import asyncio
import json
import os
import datetime
import logging
from datetime import timedelta, timezone

import config

logger = logging.getLogger(__name__)

class StateManager:
    def __init__(self, filename):
        self.filename = filename
        self.lock = asyncio.Lock()
        self.state = self._load_initial()

    def _load_initial(self):
        default_state = {
            "chat_history": [],
            "alarms": [],
            "last_interaction": datetime.datetime.now(timezone.utc).timestamp(),
            "summary": "",
            "messages_since_summary": 0
        }
        if os.path.exists(self.filename):
            try:
                with open(self.filename, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                # Миграция старых данных — дозаполнить недостающие ключи дефолтами
                for key, value in default_state.items():
                    data.setdefault(key, value)
                # Миграция старого task_list -> alarms (high) , low-priority интересы отбрасываются
                if data.get("task_list"):
                    now_ts = datetime.datetime.now(timezone.utc).timestamp()
                    for t in data["task_list"]:
                        text = t.get("text", "без темы")
                        task_id = t.get("id") or os.urandom(4).hex()
                        if t.get("priority") == "high":
                            data["alarms"].append({
                                "id": task_id,
                                "text": text,
                                "due_ts": t.get("due_time") or (now_ts + 3600),
                                "missed": 0,
                                "context": ""
                            })
                    data.pop("task_list", None)
                    data.pop("due_time", None)
                    # Синхронно сохраняем, чтобы миграция прошла ровно один раз
                    with open(self.filename, 'w', encoding='utf-8') as f:
                        json.dump(data, f, ensure_ascii=False, indent=2)
                    logger.info("🔄 [STATE] Миграция task_list -> alarms выполнена")
                return data
            except (json.JSONDecodeError, IOError) as e:
                logger.error(f"Ошибка загрузки state.json: {e}. Создаю новый.")
        return default_state

    async def save(self):
        async with self.lock:
            temp_file = self.filename + ".tmp"
            try:
                with open(temp_file, 'w', encoding='utf-8') as f:
                    json.dump(self.state, f, ensure_ascii=False, indent=2)
                os.replace(temp_file, self.filename)
            except OSError as e:
                logger.error(f"❌ [STATE] Не удалось сохранить state.json: {e}")
                self.state["sys_notice"] = "Системный сбой: не удалось сохранить память (вероятно, кончилось место на диске). Сообщения обрабатываются, но могут потеряться при перезапуске. Скажи Пете, что что-то сбоит с памятью, и попроси повторить просьбу позже."

    async def add_history(self, role, text):
        msk = datetime.timezone(datetime.timedelta(hours=3))
        ts = datetime.datetime.now(msk).strftime("%d.%m %H:%M")
        new_message = {"role": role, "content": text, "ts": ts}
        # Краткосрочная память для промпта
        self.state["chat_history"].append(new_message)
        self.state["chat_history"] = self.state["chat_history"][-config.CHAT_HISTORY_LIMIT:]

        # Счётчик для триггера обновления саммари (каждые 50 сообщений)
        self.state["messages_since_summary"] = self.state.get("messages_since_summary", 0) + 1
        await self.save()

    async def add_tool_record(self, record_text):
        """Запись вызова инструмента (server/search/memory).
        Идёт В chat_history (чтобы бот видел свои действия в истории)."""
        msk = datetime.timezone(datetime.timedelta(hours=3))
        ts = datetime.datetime.now(msk).strftime("%d.%m %H:%M")
        new_message = {"role": "tool", "content": record_text, "ts": ts}
        self.state["chat_history"].append(new_message)
        self.state["chat_history"] = self.state["chat_history"][-config.CHAT_HISTORY_LIMIT:]
        self.state["messages_since_summary"] = self.state.get("messages_since_summary", 0) + 1
        await self.save()

    async def set_summary(self, summary_text):
        self.state["summary"] = summary_text
        self.state["messages_since_summary"] = 0
        await self.save()
        logger.info(f"📌 [SUMMARY] Саммари обновлено ({len(summary_text)} символов)")

    async def add_alarm(self, text, minutes, context=""):
        due_ts = (datetime.datetime.now(timezone.utc) + timedelta(minutes=minutes)).timestamp()
        alarm_id = os.urandom(4).hex()

        for a in self.state.get("alarms", []):
            if a["text"] == text:
                logger.info(f"⚠️ [ALARM] Дубликат будильника '{text}' пропущен. Возвращаю существующий id.")
                return a["id"]

        self.state.setdefault("alarms", []).append({
            "id": alarm_id,
            "text": text,
            "due_ts": due_ts,
            "missed": 0,
            "context": context
        })
        logger.info(f"✅ [ALARM] Новый будильник '{text}' сработает через {minutes} мин. id={alarm_id}")
        await self.save()
        return alarm_id

    async def remove_alarm(self, alarm_id):
        initial_len = len(self.state.get("alarms", []))
        self.state["alarms"] = [a for a in self.state["alarms"] if a.get("id") != alarm_id]
        if len(self.state["alarms"]) < initial_len:
            logger.info(f"🗑️ [ALARM] Будильник {alarm_id} удалён.")
            await self.save()
            return True
        return False

    async def remove_alarm_by_text(self, text):
        target = None
        for a in self.state.get("alarms", []):
            if a["text"] == text:
                target = a
                break
        if not target:
            logger.info(f"🗑️ [ALARM] Не нашёл будильник '{text}' — удалять нечего.")
            return False
        return await self.remove_alarm(target["id"])

    async def edit_alarm(self, alarm_id, text=None, minutes=None):
        alarms = self.state.get("alarms", [])
        for a in alarms:
            if a.get("id") == alarm_id:
                if text is not None:
                    a["text"] = text
                if minutes is not None:
                    a["due_ts"] = (datetime.datetime.now(timezone.utc) + timedelta(minutes=int(minutes))).timestamp()
                logger.info(f"✏️ [ALARM] Будильник {alarm_id} отредактирован: text={a['text']!r}, due={a['due_ts']}")
                await self.save()
                return True
        logger.info(f"✏️ [ALARM] Не нашёл будильник {alarm_id} — редактировать нечего.")
        return False

    async def edit_alarm_by_text(self, text, new_text=None, minutes=None):
        for a in self.state.get("alarms", []):
            if a["text"] == text:
                if new_text is not None:
                    a["text"] = new_text
                if minutes is not None:
                    a["due_ts"] = (datetime.datetime.now(timezone.utc) + timedelta(minutes=int(minutes))).timestamp()
                logger.info(f"✏️ [ALARM] Будильник по тексту '{text}' отредактирован: text={a['text']!r}, due={a['due_ts']}")
                await self.save()
                return True
        logger.info(f"✏️ [ALARM] Не нашёл будильник '{text}' — редактировать нечего.")
        return False

    async def bump_alarm_missed(self, alarm):
        alarm["missed"] += 1
        if alarm["missed"] >= config.ALARM_MAX_MISSES:
            logger.warning(f"⏰ [ALARM] Будильник '{alarm['text']}' пропущен {alarm['missed']} раз — удаляю.")
            await self.remove_alarm(alarm["id"])
        else:
            logger.info(f"⏰ [ALARM] Будильник '{alarm['text']}' пропущен ({alarm['missed']}/{config.ALARM_MAX_MISSES}), держится как 🔔❌")
            await self.save()

    def build_context_snippet(self, n=8):
        recent = self.state["chat_history"][-n:]
        if not recent:
            return ""
        return "\n".join([f"[{m.get('ts','')}] {'Юзер' if m['role']=='user' else 'Бот'}: {m['content']}" for m in recent])
            
    async def update_interaction(self):
        self.state["last_interaction"] = datetime.datetime.now(timezone.utc).timestamp()
        await self.save()

    def get_msk_time_obj(self):
        return datetime.datetime.now(timezone.utc) + timedelta(hours=3)

    def get_msk_time_str(self):
        dt = self.get_msk_time_obj()
        weekdays = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
        return dt.strftime("%-d.%m.%y") + ", " + weekdays[dt.weekday()] + ". " + dt.strftime("%H:%M")