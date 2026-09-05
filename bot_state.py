# --- START OF FILE bot_state.py ---

import asyncio
import json
import os
import datetime
import random
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
            "interests": [],
            "last_interest_attempt": 0,
            "base_mood": 0.55, 
            "spike": 0.0, 
            "residual": 0.0,
            "last_physics_update": 0,
            "last_interaction": datetime.datetime.now(timezone.utc).timestamp(),
            "background_thoughts": [],
            "last_reflection_time": 0,
            "is_at_peak": False,
            "reflection_history": [],
            "summary": "",
            "messages_since_summary": 0
        }
        if os.path.exists(self.filename):
            try:
                with open(self.filename, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                # Миграция старых данных если нужно
                for key, value in default_state.items():
                    data.setdefault(key, value)
                # Миграция старого task_list -> alarms (high) / interests (low)
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
                        else:
                            data["interests"].append({
                                "id": task_id,
                                "text": text
                            })
                    data.pop("task_list", None)
                    data.pop("due_time", None)
                    # Синхронно сохраняем, чтобы миграция прошла ровно один раз
                    with open(self.filename, 'w', encoding='utf-8') as f:
                        json.dump(data, f, ensure_ascii=False, indent=2)
                    logger.info("🔄 [STATE] Миграция task_list -> alarms/interests выполнена")
                return data
            except (json.JSONDecodeError, IOError) as e:
                logger.error(f"Ошибка загрузки state.json: {e}. Создаю новый.")
        return default_state

    async def save(self):
        async with self.lock:
            temp_file = self.filename + ".tmp"
            with open(temp_file, 'w', encoding='utf-8') as f:
                json.dump(self.state, f, ensure_ascii=False, indent=2)
            os.replace(temp_file, self.filename)

    async def add_history(self, role, text):
        msk = datetime.timezone(datetime.timedelta(hours=3))
        ts = datetime.datetime.now(msk).strftime("%H:%M")
        new_message = {"role": role, "content": text, "ts": ts}
        # Краткосрочная память для промпта
        self.state["chat_history"].append(new_message)
        self.state["chat_history"] = self.state["chat_history"][-config.CHAT_HISTORY_LIMIT:]
        
        # Долгосрочная память для рефлексии
        self.state.setdefault("reflection_history", []).append(new_message)
        self.state["reflection_history"] = self.state["reflection_history"][-400:]

        # Счётчик для триггера обновления саммари (каждые 50 сообщений)
        self.state["messages_since_summary"] = self.state.get("messages_since_summary", 0) + 1
        await self.save()

    async def add_tool_record(self, record_text):
        """Запись вызова инструмента (server/search/memory).
        Идёт В chat_history (чтобы бот видел свои действия в истории),
        но НЕ в reflection_history (для рефлексии не нужно)."""
        msk = datetime.timezone(datetime.timedelta(hours=3))
        ts = datetime.datetime.now(msk).strftime("%H:%M")
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

    async def add_interest(self, text):
        for i in self.state.get("interests", []):
            if i["text"] == text:
                logger.info(f"⚠️ [INTEREST] Дубликат темы '{text}' пропущен.")
                return

        self.state.setdefault("interests", []).append({
            "id": os.urandom(4).hex(),
            "text": text
        })
        logger.info(f"✅ [INTEREST] Новая тема/намерение: '{text}'")
        await self.save()

    async def try_interest_followup(self, cooldown_minutes, silence_minutes, now_ts=None):
        """Можно ли сейчас запускать follow-up по темам?
        Условия: есть темы, тишина в чате дольше silence_minutes,
        и с последней попытки прошло больше cooldown_minutes.
        Возвращает (можно, почему нельзя)."""
        now_ts = now_ts or datetime.datetime.now(timezone.utc).timestamp()
        has_pending = any(t.get("pending") for t in self.state.get("background_thoughts", []))
        if not self.state.get("interests") and not has_pending:
            return False, "нет тем"
        last_interaction = self.state["last_interaction"]
        if (now_ts - last_interaction) <= silence_minutes * 60:
            return False, "идёт активный диалог"
        last_attempt = self.state.get("last_interest_attempt", 0)
        if (now_ts - last_attempt) <= cooldown_minutes * 60:
            return False, f"кулдаун ({cooldown_minutes} мин) не прошёл"
        return True, ""

    async def mark_interest_attempt(self):
        self.state["last_interest_attempt"] = datetime.datetime.now(timezone.utc).timestamp()
        await self.save()

    async def remove_interest(self, interest_id):
        initial_len = len(self.state.get("interests", []))
        self.state["interests"] = [i for i in self.state["interests"] if i.get("id") != interest_id]
        if len(self.state["interests"]) < initial_len:
            logger.info(f"🗑️ [INTEREST] Тема {interest_id} удалена.")
            await self.save()
            return True
        return False

    async def remove_interests(self, interest_ids):
        alive = [i for i in self.state.get("interests", []) if i.get("id") not in interest_ids]
        if len(alive) != len(self.state.get("interests", [])):
            self.state["interests"] = alive
            logger.info(f"🗑️ [INTEREST] Удалены выполненные темы: {interest_ids}")
            await self.save()

    def build_context_snippet(self, n=8):
        recent = self.state["chat_history"][-n:]
        if not recent:
            return ""
        return "\n".join([f"[{m.get('ts','')}] {'Юзер' if m['role']=='user' else 'Бот'}: {m['content']}" for m in recent])
            
    def get_total_mood(self):
        return max(0.0, self.state["base_mood"] + self.state["spike"] + self.state["residual"])

    async def apply_reaction(self, shift):
        self.state["spike"] += shift
        self.state["residual"] += (shift * 0.5)
        # Не давать суммарному настроению уходить ниже нуля
        low = self.state["base_mood"] + self.state["spike"] + self.state["residual"]
        if low < 0.0:
            self.state["spike"] -= low
        await self.save()
        logger.info(f"💥 [REACTION] Shift: {shift:+.2f} | Total: {self.get_total_mood():.2f}")

    async def check_and_apply_peak_decay(self):
        if self.state["is_at_peak"]:
            logger.info("📉 [PSYCHOLOGY] Эйфория спадает.")
            self.state["spike"] *= 0.1
            self.state["residual"] = min(self.state["residual"], 0.3)
            self.state["is_at_peak"] = False
            await self.save()

    def get_mood_instruction(self):
        m = self.get_total_mood()
        feel = (
            f"Важно: настроение влияет ТОЛЬКО на тон и эмоциональную окраску. "
            f"Качество и полнота работы при этом всегда одинаковые: задачи, код, "
            f"проверки, поиск, чтение файлов выполняй одинаково тщательно и полно "
            f"в любом настроении."
        )
        if m >= 1.0:
            self.state["is_at_peak"] = True
            return "ТВОЕ СОСТОЯНИЕ: ✨ Эйфория. СТИЛЬ: Ты на пике эмоций! Будь очень открытым. Можно использовать `)))` для радости, но уместно, не постоянно.\n" + feel
        elif m < 0.30:
            return "ТВОЕ СОСТОЯНИЕ: ☁️ Уставшее. СТИЛЬ: Пиши кратко, без эмоций, но задачи выполняй так же тщательно.\n" + feel
        elif m < 0.65:
            return "ТВОЕ СОСТОЯНИЕ: 🌤️ Нормальное. СТИЛЬ: Обычный дружеский диалог.\n" + feel
        else:
            return "ТВОЕ СОСТОЯНИЕ: ☀️ Хорошее. СТИЛЬ: Дружелюбный. Можно использовать `)` в конце фразы для теплоты, но не в каждом сообщении.\n" + feel

    async def add_thoughts(self, thoughts):
        if not thoughts:
            return
        for thought in thoughts:
            if isinstance(thought, str):
                text, pending = thought, False
            elif isinstance(thought, dict):
                text = (thought.get("text") or "").strip()
                pending = bool(thought.get("pending"))
            else:
                continue
            if not text:
                continue
            # Генерация уникального ID
            self.state["background_thoughts"].append({"id": os.urandom(4).hex(), "text": text, "pending": pending})
            logger.info(f"💡 [REFLECTION] Сгенерирована новая мысль: {text}" + (" (ждёт выхода — намерение)" if pending else ""))
        # Ротация: храним только последние MAX_BACKGROUND_THOUGHTS
        max_n = config.MAX_BACKGROUND_THOUGHTS
        if len(self.state["background_thoughts"]) > max_n:
            self.state["background_thoughts"] = self.state["background_thoughts"][-max_n:]
        self.state["last_reflection_time"] = datetime.datetime.now(timezone.utc).timestamp()
        await self.save()

    async def remove_thought(self, thought_id):
        self.state["background_thoughts"] = [t for t in self.state["background_thoughts"] if t["id"] != thought_id]
        logger.info(f"💡 [REFLECTION] Мысль {thought_id} была использована.")
        await self.save()

    async def update_physics(self):
        # Естественное затухание эмоций, привязанное ко времени (полураспад),
        # чтобы не зависеть от того, как часто вызывается этот метод.
        now = datetime.datetime.now(timezone.utc).timestamp()
        last = self.state.get("last_physics_update")
        if last is None:
            self.state["last_physics_update"] = now
            await self.save()
            return
        elapsed_h = max(0.0, (now - last) / 3600.0)
        self.state["last_physics_update"] = now

        # Очень частые вызовы (меньше ~2 минут) почти ничего не меняют — пропускаем запись
        if elapsed_h < 0.03:
            return

        # Всплеск гаснет быстро: полураспад ~45 минут
        self.state["spike"] *= 0.5 ** (elapsed_h / 0.75)
        # Шлейф гаснет медленно: полураспад ~8 часов
        self.state["residual"] *= 0.5 ** (elapsed_h / 8.0)
        # Дрейф базового настроения к «норме», обратно пропорционально времени
        target_base = 0.60
        self.state["base_mood"] += (
            (target_base - self.state["base_mood"])
            * min(1.0, elapsed_h * 0.05)
            + random.uniform(-0.01, 0.01)
        )
        self.state["base_mood"] = max(0.2, min(0.9, self.state["base_mood"]))
        await self.save()
        logger.info(f"🌊 [PHYSICS] Total mood: {self.get_total_mood():.2f}")

    async def update_interaction(self):
        self.state["last_interaction"] = datetime.datetime.now(timezone.utc).timestamp()
        await self.save()

    def get_msk_time_obj(self):
        return datetime.datetime.now(timezone.utc) + timedelta(hours=3)

    def get_msk_time_str(self):
        dt = self.get_msk_time_obj()
        weekdays = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
        return dt.strftime("%-d.%m.%y") + ", " + weekdays[dt.weekday()] + ". " + dt.strftime("%H:%M")