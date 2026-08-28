import os
import asyncio
import logging

os.environ["no_proxy"] = "127.0.0.1,localhost"
os.environ["NO_PROXY"] = "127.0.0.1,localhost"

from memorylite import MemoryAgent, MemoryAgentConfig
from memorylite.llm import OpenAICompatibleJSONClient
from memorylite.schema import ChatMessage, RecallDecision, RecallItem, RecallResult, now_ts
from memorylite.compiler import ContextCompiler

logger = logging.getLogger(__name__)

WORKING_DIR = "/root/tg_bot/rag_storage"
DB_NAME = "memorylite.sqlite3"

GEMINI_PROXY_URL = os.getenv("GEMINI_PROXY_URL", "http://127.0.0.1:4984")
GEMINI_PROXY_KEY = os.getenv("GEMINI_PROXY_KEY", "sk-gemini")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.6-flash")

DEEPSEEK_URL = "http://127.0.0.1:9655"
DEEPSEEK_KEY = "sk-freedeepseek"
DEEPSEEK_MODEL = "deepseek-chat"

_agent = None


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

    client = OpenAICompatibleJSONClient(
        model=DEEPSEEK_MODEL,
        base_url=f"{DEEPSEEK_URL}/v1",
        api_key=DEEPSEEK_KEY,
    )

    _agent = MemoryAgent(config=config, client=client)
    logger.info("✅ memorylite инициализирован")
    return _agent


async def insert_to_rag(text, metadata=""):
    try:
        loop = asyncio.get_running_loop()
        agent = _get_agent()

        session_id = "tg_bot_main"

        def _remember():
            agent.remember(
                session_id=session_id,
                user_message=text,
                assistant_message="",
            )

        await loop.run_in_executor(None, _remember)
    except Exception as e:
        logger.error(f"❌ Ошибка insert_to_rag: {e}")


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
