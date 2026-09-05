import os
import asyncio
import logging
import time
import uuid
import urllib.parse

os.environ["no_proxy"] = "127.0.0.1,localhost"
os.environ["NO_PROXY"] = "127.0.0.1,localhost"

from memorylite import MemoryAgent, MemoryAgentConfig
from memorylite.llm import OpenAICompatibleJSONClient
from memorylite.schema import ChatMessage, ExtractionResult, RecallDecision, RecallItem, RecallResult, now_ts
from memorylite.compiler import ContextCompiler

logger = logging.getLogger(__name__)

# ── Monkeypatch: кириллица в memorylite._search_terms ──────────────────────
# memorylite извлекает только латиницу и CJK → русские запросы дают пустой
# FTS-запрос,levance = 0, fallback не находит совпадений.
# Патчим store._search_terms (FTS-кандидаты) и agent._search_terms (relevance).
import re as _re

def _patched_search_terms(self, text: str) -> list[str]:
    """Извлекает ключевые слова: кириллица (≥3), CJK (≥2), латиница (≥3)."""
    lower = text.lower()
    cyrillic = _re.findall(r"[а-яё][а-яё0-9_-]{2,19}", lower)
    cjk = _re.findall(r"[\u4e00-\u9fff]{2,8}", text)
    latin = _re.findall(r"[a-zA-Z][a-zA-Z0-9_-]{2,20}", lower)
    return list(dict.fromkeys(cyrillic[:12] + cjk[:12] + latin[:12]))

from memorylite.store import SQLiteStore
SQLiteStore._search_terms = _patched_search_terms
MemoryAgent._search_terms = _patched_search_terms

# ── Monkeypatch: нормализация фактов (нейтральные «замки» для вопросов-«ключей») ──
# Штатный промпт memorylite велит «preserve the user's original statement literally»,
# из-за чего в память попадают дословные реплики («Бот: тебя зовут Петя»), а не чистые
# факты («Пользователя зовут Петя»). Это ломает ответ на вопрос бота «как меня зовут».
# Переопределяем ModelMemoryController.extract_memories, чтобы факты записывались
# нейтрально и однозначно: определяется субъект (бот/пользователь) и нормализованная формулировка.
from memorylite.llm.model_controller import ModelMemoryController

_WINDOW_EXTRACTION_PROMPT = """Ты — аккуратный агент записи долговременной памяти.

Тебе дают окно последних сообщений диалога между пользователем и ассистентом-ботом,
каждое с префиксом роли («Пользователь: …» или «Бот: …») и времени («[ЧЧ:ММ]» — время МСК).

Сообщения даны в ХРОНОЛОГИЧЕСКОМ порядке: первая строка в окне — самое раннее по времени сообщение, последняя строка — самое позднее по времени (по возрастанию времени `[ЧЧ:ММ]` сверху вниз).
Чем позже сообщение по времени — тем более АКТУАЛЬНОЕ состояние оно отражает.
Если в окне сначала говорится одно, а позже — противоположное или уточняющее, то приоритет у ПОСЛЕДНЕГО (самого позднего по времени) сообщения: оно показывает итог.
Фиксируй факты/статус по последнему сообщению, а не по раннему.

Извлеки из этого окна ТОЛЬКО самые важные долговечные факты — те, которые стоит помнить долго:
- реальные факты о пользователе (его предпочтения, вкусы, профессия, значимые события жизни, черты характера, ПРЯМО сказанные пользователем);
- реальные факты о боте (его возможности/предпочтения, если они ПРЯМО зафиксированы в диалоге);
- принятые решения и договорённости (только те, что реально были решены в окне — и только если ПОЛЬЗОВАТЕЛЬ явно подтвердил согласие/действие, а не просто бот предложил);
Не сохраняй НИЧЕГО про задачи, планы, ожидания, статусы «в процессе/ожидает/сделано» — задачи и планы устаревают, память про них не веди (это касается и подтверждённых итогов: «работает», «готово», «сделано» — тоже не сохраняй, это временное состояние, а не долговечный факт).
Планы, предложения и рекомендации бота сами по себе фактами НЕ являются — это не действия и не решения пользователя.

ПРИНЦИП БУКВАЛЬНОСТИ (главное правило, важнее всего ниже):
Ты записываешь ТОЛЬКО то, что пользователь и бот сказали ЯВНО, своими словами. Никаких догадок, выводов, подтекста, интерпретаций, домысливания.
Любое слово, которого не было в диалоге, — это ошибка. Если сомневаешься — НЕ сохраняй («Лучше ничего, чем мусор»).
Примеры категорически запрещённого домысливания:
- Пользователь сказал «у меня есть ноут, буду на нём работать» → НЕЛЬЗЯ записать «купил ноут». Наличие ≠ покупка. Есть непроверяемый факт: «у пользователя есть ноут», а всё остальное не сказано.
- Бот предложил/посоветовал вариант решения, пользователь не ответил или проигнорил → НЕЛЬЗЯ записать, что пользователь так сделал. Предложение бота ≠ действие пользователя.
- Планы и предложения БОТА НЕ ЯВЛЯЮТСЯ ИСТИННЫМИ ФАКТАМИ. «Бот предложил X» не значит «X сделано» / «X решено» / «пользователь согласился». Если пользователь не подтвердил словами, что сделал/выполнил/согласился — не сохраняй как совершённое.
- Намерения, планы и обещания пользователя («собираюсь», «надо», «хочу», «буду») — это НЕ совершённые факты. Их можно зафиксировать как «Пользователь сказал, что собирается/планирует X» ТОЛЬКО если он сказал это прямо, но никогда как «X сделан/куплен/установлен».

ЖЁСТКИЕ ЗАПРЕТЫ (НЕ сохраняй это никогда):
- ИМЕНА И ПОСТОЯННЫЕ ФАКТЫ, которые уже известны: как зовут пользователя (Петя) и бота (Левий) — это уже зафиксировано, НЕ записывай повторно.
- МЕТА-РЕФЛЕКСИЮ И ВНУТРЕННИЕ СОСТОЯНИЯ БОТА: «Бот признал сбой», «Бот осознал ошибку», «Бот испытал замешательство/самокритику», «Бот запутался», «у бота нет доступа/нет воспоминаний». Это не долговременные факты, это мысли процесса — НЕ сохраняй.
- ОТСУТСТВИЕ/ОТРИЦАНИЕ: «не помню», «нет воспоминаний», «не знает», «не имеет доступа» — отсутствие знания НЕ является фактом для памяти.
- НЕ ДОДУМЫВАЙ И НЕ ЧИТАЙ ПОДТЕКСТ: не выводи психологию, намерения и чувства пользователя, если он не сказал их прямо. Запрещено: «пользователь предпочитает…», «пользователь планирует/хочет/намерен…», «пользователь отреагировал поддержкой…» — если пользователь не сказал это явно. Только буквально сказанное.
- ОДНОРАЗОВЫЕ ЭПИЗОДЫ И ДРАМЫ: разовое замешательство, путаница, извинения, «отпустило», «переклинило» — не сохранять.
- РАЗГОВОР О САМОЙ СИСТЕМЕ ПАМЯТИ/ПРОЦЕССЕ ЕЁ СОЗДАНИЯ: «обсуждаем как улучшить память», «давай доделаем микро-сервис памяти» — это техпроцесс, НЕ факт для долгосрочной памяти.
- ПРИВЕТСТВИЯ, «окей», «понял», «спасибо», короткие переспросы, «доброе утро», «как дела».
УЖАСНЫЕ ПРИМЕРЫ — так сохранять НЕЛЬЗЯ (реальные ошибки памятного правила):
- «Пользователь использует модель эмбеддингов nvidia/llama-nemotron-embed-vl-1b-v2:free через OpenRouter для векторной памяти бота; задержка на текущий момент не наблюдается» — это техническая деталь реализации/процесса, не стойкий факт о пользователе. НЕ сохраняй такие.
- «Пользователь экспериментирует с разными настройками бота и следит, чтобы тот не сломался» — общее ничего-не-значащее, без конкретного факта. НЕ сохраняй размытое.

Сохраняй только то, что потом нужно будет вспомнить. Примеры: 

ФАКТЫ:
- «Пользователь живет в X»
- «Пользователь занимается X»
- «Бот заинтересовался X»

ПРЕДПОЧТЕНИЯ:
- «Пользователь предпочитает/хочет X»
- «Бот предпочитает/хочет X»

НЕ сохраняй: события/эпизоды, сводки обсуждений, текущие задачи/планы/ожидания, итоги и смену состояний («сделано», «работает», «закрыто», «ожидает») — они временные, память про них не веди.

Если во всём окне нет ни одного подтвержденного стойкого факта — верни memories: []. Лучше ничего, чем мусор.

ВНИМАНИЕ — СУЩЕСТВУЮЩИЕ ФАКТЫ УЖЕ В БАЗЕ:
В поле existing_facts (в конце запроса) перечислены все факты, УЖЕ сохранённые в памяти, каждый со своим id.
ТВОЯ ЗАДАЧА — НЕ создавать дубликаты и не плодить противоречия:
- Если новый факт из окна по смыслу УЖЕ отражён в существующем — НЕ создавай новый объект memories для него.
- Если новый факт ОБНОВЛЯЕТ/ЗАМЕНЯЕТ существующий (например, уточнил/исправил прошлое: «живёт в X» → «живёт в Y») — создай один объект memories с актуальным содержанием И укажи supersedes_memory_id = id того существующего факта, который он заменяет. Старый факт после этого будет удалён.
- Если новый факт — это что-то принципиально новое, не касающееся существующих фактов, supersedes_memory_id оставь null.

Верни строгий JSON с ключами:
state_patch (object, можно пустой {}), memories (list[object]).

Каждый объект memories обязан содержать: scope, scope_id_key, kind, content, summary, tags, importance, confidence, supersedes_memory_id.
- scope_id_key — одно из значений ровно: session, user, project.
- kind — одно из (ТОЛЬКО эти два, никаких других):
  • fact — реальный стойкий факт о пользователе или боте (профессия, место жительства, черта характера, возможности).
  • preference — предпочтение, вкус, привычка, что нравится/не нравится.
- importance — число от 0 до 1 (насколько это важно помнить долго; 0.9+ для очень важного).
- confidence — число от 0 до 1.
- supersedes_memory_id — int id существующего факта, который этот факт заменяет (или null, если никого не заменяет).

ПРИМЕР ПРАВИЛЬНОГО JSON-ВЫВОДА (заполни по аналогии, верни ровно такую структуру):
{
  "state_patch": {},
  "memories": [
    {
      "scope": "user",
      "scope_id_key": "user",
      "kind": "preference",
      "content": "Пользователь предпочитает краткие ответы",
      "summary": "предпочитает краткие ответы",
      "tags": ["предпочтения"],
      "importance": 0.85,
      "confidence": 0.8,
      "supersedes_memory_id": null
    }
  ]
}
Если подходящих фактов нет — верни {"state_patch": {}, "memories": []}.

ФОРМУЛИРОВКА — НЕЙТРАЛЬНАЯ, с явным указанием субъекта:
  • о пользователе → «Пользователь предпочитает …», «У пользователя …», «Пользователь — …»;
  • о боте → «Бот использует …», «Бот умеет …»;
  • общее/о проекте → «Проект …», «Система …», «Сообщение …».
НИКОГДА не копируй дословные реплики («Бот: …», «Пользователь: …») в content.
Отвечай фактами, а не цитатами из диалога.
"""

_ORIG_EXTRACT = ModelMemoryController.extract_memories


def _patched_extract_memories(self, session_id, user_message, assistant_message, recent_messages, existing_state, scope_ids):
    existing_facts = []
    if isinstance(existing_state, dict):
        existing_facts = existing_state.pop("__existing_facts__", []) or []
    try:
        payload = self.client.complete_json(
            system_prompt=_WINDOW_EXTRACTION_PROMPT,
            user_prompt=(
                f"user_message={user_message!r}\n"
                f"assistant_message={assistant_message!r}\n"
                f"recent_messages={[{'role': m.role, 'content': m.content[:120]} for m in recent_messages[-self.config.recent_prompt_window:]]!r}\n"
                f"existing_state={existing_state!r}\n"
                f"existing_facts={existing_facts!r}\n"
                f"scope_ids={scope_ids!r}"
            ),
        )
    except Exception:
        return ExtractionResult()

    raw_memories = payload.get("memories", [])
    if not isinstance(raw_memories, list):
        raw_memories = []

    memories = []
    seen = set()
    for item in raw_memories:
        memory = self._build_memory_record(
            item=item,
            raw_memory_count=len(raw_memories),
            user_message=user_message,
            assistant_message=assistant_message,
            scope_ids=scope_ids,
        )
        if memory is None:
            continue
        try:
            sid = item.get("supersedes_memory_id")
            if isinstance(sid, (int, float)) and not isinstance(sid, bool):
                memory.supersedes_memory_id = int(sid)
        except Exception:
            pass
        key = (memory.scope, memory.scope_id, memory.kind, memory.content.lower())
        if key in seen:
            continue
        seen.add(key)
        memories.append(memory)

    state_patch = payload.get("state_patch", {}) or {}
    if not isinstance(state_patch, dict):
        state_patch = {}
    return ExtractionResult(memories=memories, state_patch=state_patch)


ModelMemoryController.extract_memories = _patched_extract_memories
# ───────────────────────────────────────────────────────────────────────────

import providers

# ── Новый слой: векторные эмбеддинги фактов (НЕ трогает recall / саммари) ──
# Модель/URL/ключ берутся из провайдеров (providers.EMBED_*) — настраивается в /models → «Эмбеддинги».
EMBED_MAX_BATCH = 16


def _embed_model():
    """Актуальная модель эмбеддингов из конфига провайдеров."""
    try:
        return providers.EMBED_MODEL or "liquid/lfm-2.5-embedding-350m:free"
    except Exception:
        return "liquid/lfm-2.5-embedding-350m:free"


def _embed_batch(texts):
    """Отправляет список строк в эмбеддер (провайдер из providers.EMBED_*), возвращает list[list[float]].
    Длины совпадают с входом. При пустом входе — пустой список."""
    texts = [t for t in texts if isinstance(t, str) and t.strip()]
    if not texts:
        return []
    vecs = _embed_one(texts, providers.EMBED_URL, providers.EMBED_KEY, _embed_model())
    if vecs:
        return vecs
    # Фолбек (если настроен в /models → «Эмбеддинги»)
    if providers.EMBED_FALLBACK_URL:
        logger.info("🆘 [EMBED] Основной эмбеддер не сработал, пробую фолбек")
        vecs = _embed_one(texts, providers.EMBED_FALLBACK_URL, providers.EMBED_FALLBACK_KEY, providers.EMBED_FALLBACK_MODEL)
        if vecs:
            return vecs
    return []


def _embed_one(texts, url, key, model):
    """Один вызов эмбеддера с ретраями. Возвращает list[list[float]] или None при отказе."""
    for attempt in range(3):
        try:
            return providers.embed(url, key, model, texts, timeout=90)
        except Exception as e:
            logger.error(f"🧠 [EMBED] Ошибка (попытка {attempt + 1}): {e}")
            if attempt < 2:
                time.sleep(2)
    return None


def _cosine(a, b):
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(x * x for x in b) ** 0.5
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


WORKING_DIR = "/root/tg_bot/rag_storage"
DB_NAME = "memorylite.sqlite3"

RAG_URL = providers.RAG_URL
RAG_KEY = providers.RAG_KEY
RAG_MODEL = providers.RAG_MODEL

_agent = None


class FreshSessionJSONClient(OpenAICompatibleJSONClient):
    """Каждый вызов DeepSeek — свежая сессия (уникальный user), после ответа удаляется с DeepSeek и с диска."""

    def complete_json(self, system_prompt, user_prompt):
        import json
        import urllib.request

        user_id = f"tg_bot_rag_{uuid.uuid4().hex[:12]}"
        payload = {
            "model": self.model,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "user": user_id,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        request = urllib.request.Request(
            url=f"{self.base_url}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with providers.open_url(
                request,
                timeout=self.timeout_seconds,
                proxy=providers.proxy_for_url(self.base_url),
            ) as response:
                result = json.loads(response.read().decode("utf-8"))
            content = result["choices"][0]["message"]["content"]
            parsed = self._parse_json_payload(content)
        finally:
            self._delete_session(user_id)
        return parsed

    def _delete_session(self, user_id):
        # Удаление сессий умеет только freedeepseek-api (порт 9655).
        # Остальных провайдеров (GPT/Gemini/g4f и т.п.) не трогаем.
        try:
            base = self.base_url.rsplit("/v1", 1)[0]
            base_hostport = (urllib.parse.urlsplit(base).hostname, urllib.parse.urlsplit(base).port)
            if base_hostport not in (("localhost", 9655), ("127.0.0.1", 9655)):
                return
            req = urllib.request.Request(
                url=f"{base}/session?agent={urllib.parse.quote(user_id)}",
                method="DELETE",
            )
            with providers.open_url(req, timeout=10) as resp:
                resp.read()
        except Exception as e:
            logger.warning(f"⚠️ Не удалил сессию LLM {user_id}: {e}")


def _get_agent():
    global _agent
    if _agent is not None:
        return _agent

    os.makedirs(WORKING_DIR, exist_ok=True)

    config = MemoryAgentConfig(
        root_dir=WORKING_DIR,
        database_name=DB_NAME,
        max_recall_items=15,
        candidate_pool_size=30,
        background_write=False,
    )

    client = FreshSessionJSONClient(
        model=RAG_MODEL,
        base_url=f"{RAG_URL.rstrip('/').rsplit('/v1', 1)[0]}/v1",
        api_key=RAG_KEY,
    )

    _agent = MemoryAgent(config=config, client=client)
    logger.info("✅ memorylite инициализирован")
    return _agent


async def remember_window(messages, session_id="tg_bot_main", max_facts=50):
    """Пакетно извлекает важные факты из окна диалога и записывает в память.
    messages — список строк вида «Пользователь: …» / «Бот: …».
    Запускается вместе с триггером саммари, а не на каждое сообщение.
    max_facts — жёсткий лимит фактов в БД: всё влезает в промпт извлекателя,
    поэтому он видит существующие факты с id и НЕ дублирует, а через
    supersedes_memory_id обновляет/заменяет устаревшие."""
    try:
        text = "\n".join(messages) if messages else ""
        if not text.strip():
            return
        loop = asyncio.get_running_loop()
        agent = _get_agent()
        scope_ids = {"session": session_id}

        def _load_existing_facts():
            rows = agent.store.conn.execute(
                "SELECT id, kind, content, summary FROM memories WHERE scope = ? AND scope_id = ? ORDER BY importance DESC, updated_at DESC LIMIT ?",
                ("session", session_id, max_facts),
            ).fetchall()
            return [{"id": int(r["id"]), "kind": r["kind"], "content": r["content"], "summary": r["summary"]} for r in rows]

        def _apply():
            try:
                existing_facts = _load_existing_facts()
                state = agent.get_state("session", session_id) or {}
                state = dict(state)
                state["__existing_facts__"] = existing_facts
                result = agent.controller.extract_memories(
                    session_id=session_id,
                    user_message=text,
                    assistant_message="",
                    recent_messages=[],
                    existing_state=state,
                    scope_ids=scope_ids,
                )
            except Exception as e:
                logger.error(f"❌ remember_window extract_memories: {e}")
                return
            memories = [m for m in result.memories if (m.importance or 0) >= 0.55]
            if not memories:
                logger.info("🧠 [MEMORY] Окно: значимых фактов не найдено, память не пополнена")
                return
            supersedes = [m.supersedes_memory_id for m in memories if m.supersedes_memory_id]
            embeddings = _embed_batch([m.content for m in memories])
            agent.store.persist_extraction(
                memories, [],
                embeddings_model=_embed_model(),
                memory_embeddings=embeddings or None,
            )
            try:
                if supersedes:
                    agent.store._delete_memories(supersedes)
            except Exception as e:
                logger.error(f"⚠️ remember_window: не удалил заменённые факты {supersedes}: {e}")
            _enforce_fact_cap(agent, session_id, max_facts)
            for m in memories:
                logger.info(f"🧠 [MEMORY] СОХРАНЕНО: {m.content}" + (f" (заменён id {m.supersedes_memory_id})" if m.supersedes_memory_id else ""))

        await loop.run_in_executor(None, _apply)
    except Exception as e:
        logger.error(f"❌ Ошибка remember_window: {e}")


TASK_KINDS = {"task", "task_state"}


def _enforce_fact_cap(agent, session_id, max_facts):
    """Если фактов в БД больше max_facts — удаляет лишние.
    Приоритет удаления: сначала задачи (task/task_state) по важности (наименее важные первыми),
    затем остальные факты по важности. Задачи устаревают быстро, поэтому выкидываются в первую очередь.
    Позволяет держать базу в пределах лимита, чтобы все факты влезали в промпт извлекателя."""
    try:
        rows = agent.store.conn.execute(
            "SELECT id, kind, importance, updated_at FROM memories WHERE scope = ? AND scope_id = ?",
            ("session", session_id),
        ).fetchall()
        total = len(rows)
        if total <= max_facts:
            return

        def sort_key(r):
            is_task = 0 if r["kind"] in TASK_KINDS else 1
            imp = r["importance"] or 0
            upd = r["updated_at"] or 0
            return (is_task, imp, upd)

        ordered = sorted(rows, key=sort_key, reverse=True)
        to_delete = [int(r["id"]) for r in ordered[max_facts:]]
        agent.store._delete_memories(to_delete)
        logger.info(f"🧠 [MEMORY] Лимит {max_facts}: удалено {len(to_delete)} фактов (сначала задачи)")
    except Exception as e:
        logger.error(f"⚠️ remember_window: сбой применения лимита фактов: {e}")


async def vector_search(query_text, top_k=5, threshold=0.30):
    """Новый слой: векторный поиск по смыслу фактов в памяти.
    Возвращает список content-ов фактов со сходством >= threshold, топ-k по убыванию.
    При ошибке/пустых данных — пустой список. Не трогает recall/саммари."""
    if not query_text or not query_text.strip():
        return []
    try:
        loop = asyncio.get_running_loop()
        agent = _get_agent()

        def _run():
            try:
                qvec = _embed_batch([query_text])
            except Exception as e:
                logger.error(f"🧠 [VECTOR] Ошибка эмбеддинга запроса: {e}")
                return []
            if not qvec or not qvec[0]:
                return []
            rows = agent.store.conn.execute(
                "SELECT id, content FROM memories WHERE scope = ? AND scope_id = ?",
                ("session", "tg_bot_main"),
            ).fetchall()
            if not rows:
                return []
            ids = [r["id"] for r in rows]
            vecs = agent.store.load_memory_embeddings(ids, _embed_model())
            scored = []
            for r in rows:
                v = vecs.get(r["id"])
                if not v:
                    continue
                s = _cosine(qvec[0], v)
                if s >= threshold:
                    scored.append((s, r["content"]))
            scored.sort(key=lambda x: x[0], reverse=True)
            return [content for _, content in scored[:top_k]]

        return await loop.run_in_executor(None, _run)
    except Exception as e:
        logger.error(f"❌ Ошибка vector_search: {e}")
        return []


_ANSWER_FORMAT_PROMPT = """Ты — модуль памяти чат-бота. Тебе даны сырые факты из базы памяти и вопрос, который бот задаёт тебе (модулю памяти).

ЗАДАЧА: по найденным фактам составь короткий ответ на вопрос бота.

ПРАВИЛА:
- Формулируй ответ ДЛЯ БОТА (не от его имени): «Тебя зовут …», «Ты говорил …», «Пользователь сказал …».
- Если фактов достаточно — ответь конкретно, 1-2 предложения.
- Если фактов мало или нет — честно скажи «В памяти ничего не нашлось».
- Не придумывай то, чего нет в фактах.
- Без воды и вводных слов."""


async def format_memory_answer(query, raw_fragments):
    """LLM-шаг: берёт сырые фрагменты из query_rag и формирует ответ для бота."""
    if not raw_fragments:
        return None
    user_prompt = f"ФРАГМЕНТЫ ИЗ ПАМЯТИ:\n{raw_fragments}\n\nВОПРОС БОТА: {query}"

    async def _one(url, key, model, tag):
        try:
            import json as _json
            import urllib.request as _req
            payload = _json.dumps({
                "model": model,
                "messages": [
                    {"role": "system", "content": _ANSWER_FORMAT_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                "temperature": 0,
            }).encode("utf-8")
            headers = {"Content-Type": "application/json"}
            if key:
                headers["Authorization"] = f"Bearer {key}"
            full_url = providers.chat_completions_url(url)
            req = _req.Request(url=full_url, data=payload, headers=headers, method="POST")
            loop = asyncio.get_running_loop()
            with providers.open_url(req, timeout=30, proxy=providers.proxy_for_url(full_url)) as response:
                data = _json.loads(response.read().decode("utf-8"))
            text = data["choices"][0]["message"]["content"].strip()
            logger.info(f"🧠 [MEMORY:{tag}] НАШЁЛ: {text}")
            return text
        except Exception as e:
            logger.error(f"❌ [MEMORY:{tag}] Ошибка: {e}")
            return None

    text = await _one(RAG_URL, RAG_KEY, RAG_MODEL, providers.get_provider_name(RAG_URL))
    if text:
        return text

    if providers.RAG_FALLBACK_URL:
        fb = await _one(providers.RAG_FALLBACK_URL, providers.RAG_FALLBACK_KEY,
                        providers.RAG_FALLBACK_MODEL, providers.get_provider_name(providers.RAG_FALLBACK_URL))
        if fb:
            return fb

    return raw_fragments


async def query_rag(query, top_k=5):
    try:
        loop = asyncio.get_running_loop()
        agent = _get_agent()

        session_id = "tg_bot_main"

        def _recall():
            return agent.recall(
                query=query,
                session_id=session_id,
                max_items=top_k,
            )

        result = await loop.run_in_executor(None, _recall)

        if result.items:
            texts = []
            for item in result.items:
                texts.append(item.content)
            return "\n".join(texts)
        return None
    except Exception as e:
        logger.error(f"❌ Ошибка query_rag: {e}")
        return None
