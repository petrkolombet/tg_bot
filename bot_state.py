# --- START OF FILE bot_state.py ---

import asyncio
import json
import os
import datetime
import random
import logging
from datetime import timedelta, timezone

logger = logging.getLogger(__name__)

class StateManager:
    def __init__(self, filename):
        self.filename = filename
        self.lock = asyncio.Lock()
        self.state = self._load_initial()

    def _load_initial(self):
        default_state = {
            "chat_history": [],
            "task_list": [], 
            "base_mood": 0.55, 
            "spike": 0.0, 
            "residual": 0.0,
            "last_interaction": datetime.datetime.now(timezone.utc).timestamp(),
            "pending_topic": None,
            "background_thoughts": [],
            "last_reflection_time": 0,
            "offense_state": {"active": False, "timestamp": 0},
            "is_at_peak": False,
            "reflection_history": []
        }
        if os.path.exists(self.filename):
            try:
                with open(self.filename, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                # Миграция старых данных если нужно
                for key, value in default_state.items():
                    data.setdefault(key, value)
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
        new_message = {"role": role, "content": text}
        # Краткосрочная память для промпта
        self.state["chat_history"].append(new_message)
        self.state["chat_history"] = self.state["chat_history"][-50:]
        
        # Долгосрочная память для рефлексии
        self.state.setdefault("reflection_history", []).append(new_message)
        self.state["reflection_history"] = self.state["reflection_history"][-400:]
        await self.save()

    async def add_task(self, text, minutes, priority):
        due_time = (datetime.datetime.now(timezone.utc) + timedelta(minutes=minutes)).timestamp()
        task_id = os.urandom(4).hex()
        
        # Простая проверка на полные дубликаты текста, чтобы не спамить
        current_tasks = self.state.get("task_list", [])
        for t in current_tasks:
            if t["text"] == text and t["priority"] == priority:
                logger.info(f"⚠️ [TASK] Дубликат задачи '{text}' пропущен.")
                return

        self.state.setdefault("task_list", []).append({
            "id": task_id,
            "text": text,
            "due_time": due_time,
            "priority": priority
        })
        logger.info(f"✅ [TASK] Новая задача '{text}' (приоритет: {priority}) запланирована через {minutes} мин.")
        await self.save()

    async def remove_task(self, task_id):
        initial_len = len(self.state.get("task_list", []))
        self.state["task_list"] = [t for t in self.state["task_list"] if t.get("id") != task_id]
        if len(self.state["task_list"]) < initial_len:
            logger.info(f"🗑️ [TASK] Задача {task_id} удалена из списка.")
            await self.save()
            return True
        return False
            
    def get_total_mood(self):
        return max(0.05, self.state["base_mood"] + self.state["spike"] + self.state["residual"])

    async def apply_reaction(self, shift):
        self.state["spike"] += shift
        self.state["residual"] += (shift * 0.5)
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
        if m >= 1.0:
            self.state["is_at_peak"] = True
            return "ТВОЕ СОСТОЯНИЕ: ✨ Эйфория. СТИЛЬ: Ты на пике эмоций! Будь очень открытым. Можно использовать `)))` для радости, но уместно, не постоянно."
        elif m < 0.30:
            return "ТВОЕ СОСТОЯНИЕ: ☁️ Уставшее. СТИЛЬ: Пиши кратко, без эмоций."
        elif m < 0.65:
            return "ТВОЕ СОСТОЯНИЕ: 🌤️ Нормальное. СТИЛЬ: Обычный дружеский диалог."
        else:
            return "ТВОЕ СОСТОЯНИЕ: ☀️ Хорошее. СТИЛЬ: Дружелюбный. Можно использовать `)` в конце фразы для теплоты, но не в каждом сообщении."

    async def set_offense_state(self, active: bool):
        self.state["offense_state"]["active"] = active
        self.state["offense_state"]["timestamp"] = datetime.datetime.now(timezone.utc).timestamp() if active else 0
        await self.save()
        if active:
            logger.warning("😡 [PSYCHOLOGY] Бот 'обиделся'.")
        else:
            logger.info("😌 [PSYCHOLOGY] Бот 'простил'.")

    def is_offended(self):
        offense = self.state["offense_state"]
        if not offense["active"]:
            return False
        if (datetime.datetime.now(timezone.utc).timestamp() - offense["timestamp"]) > 600:
            logger.info("😌 [PSYCHOLOGY] Время 'обиды' истекло.")
            offense["active"] = False
            return False
        return True

    async def set_pending_topic(self, keywords):
        self.state["pending_topic"] = {"keywords": keywords, "timestamp": datetime.datetime.now(timezone.utc).timestamp()}
        await self.save()
        logger.info(f"🧠 [THINKING] Запомнил проигнорированную тему: {keywords}")

    def get_and_clear_pending_topic(self, user_text):
        topic = self.state.get("pending_topic")
        if not topic:
            return None
        if (datetime.datetime.now(timezone.utc).timestamp() - topic["timestamp"]) > 900:
            self.state["pending_topic"] = None
            logger.info("🧠 [THINKING] Проигнорированная тема устарела.")
            return None
        if any(keyword.lower() in user_text.lower() for keyword in topic["keywords"]):
            self.state["pending_topic"] = None
            logger.info(f"🧠 [THINKING] Пользователь вернулся к теме: {topic['keywords']}.")
            return topic["keywords"]
        return None

    async def add_thoughts(self, thoughts):
        if not thoughts:
            return
        for thought in thoughts:
            # Генерация уникального ID
            self.state["background_thoughts"].append({"id": os.urandom(4).hex(), "text": thought})
            logger.info(f"💡 [REFLECTION] Сгенерирована новая мысль: {thought}")
        self.state["last_reflection_time"] = datetime.datetime.now(timezone.utc).timestamp()
        await self.save()

    async def remove_thought(self, thought_id):
        self.state["background_thoughts"] = [t for t in self.state["background_thoughts"] if t["id"] != thought_id]
        logger.info(f"💡 [REFLECTION] Мысль {thought_id} была использована.")
        await self.save()

    async def update_physics(self):
        # Естественное затухание эмоций
        self.state["spike"] *= 0.1
        self.state["residual"] *= 0.9
        # Дрейф базового настроения
        target_base = 0.60
        self.state["base_mood"] += (target_base - self.state["base_mood"]) * 0.05 + random.uniform(-0.01, 0.01)
        self.state["base_mood"] = max(0.2, min(0.9, self.state["base_mood"]))
        await self.save()
        logger.info(f"🌊 [PHYSICS] Total mood: {self.get_total_mood():.2f}")

    async def update_interaction(self):
        self.state["last_interaction"] = datetime.datetime.now(timezone.utc).timestamp()
        await self.save()

    def get_msk_time_obj(self):
        return datetime.datetime.now(timezone.utc) + timedelta(hours=3)