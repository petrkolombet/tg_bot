import os
import asyncio
import numpy as np
import urllib.request
import json

from lightrag import LightRAG, QueryParam
from lightrag.utils import EmbeddingFunc

WORKING_DIR = "/root/tg_bot/rag_storage"

JINA_API_KEY = "jina_29bc5db700b24243a91751bb0b052e93SYTgG7PJ04g69HMIMznT7MMGlETj"
GEMINI_PROXY = "http://127.0.0.1:4984"
GEMINI_KEY = "sk-gemini"
GEMINI_MODEL = "gemini-3.6-flash"


async def llm_model_func(prompt, system_prompt=None, history_messages=[], **kwargs):
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    for m in history_messages:
        messages.append({"role": m.get("role", "user"), "content": m.get("content", "")})
    messages.append({"role": "user", "content": prompt})

    payload = json.dumps({
        "model": GEMINI_MODEL,
        "messages": messages,
        "temperature": 0.3
    }).encode("utf-8")

    req = urllib.request.Request(
        f"{GEMINI_PROXY}/v1/chat/completions",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {GEMINI_KEY}"
        }
    )

    loop = asyncio.get_running_loop()
    resp = await loop.run_in_executor(None, lambda: urllib.request.urlopen(req, timeout=180))
    data = json.loads(resp.read().decode("utf-8"))
    return data["choices"][0]["message"]["content"]


async def jina_embed(texts: list[str]) -> np.ndarray:
    # Rate limiting - Jina free tier has limits
    results = []
    for i in range(0, len(texts), 5):
        batch = texts[i:i+5]
        payload = json.dumps({
            "model": "jina-embeddings-v3",
            "input": batch
        }).encode("utf-8")

        req = urllib.request.Request(
            "https://api.jina.ai/v1/embeddings",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {JINA_API_KEY}"
            }
        )

        for attempt in range(3):
            try:
                loop = asyncio.get_running_loop()
                resp = await loop.run_in_executor(None, lambda: urllib.request.urlopen(req, timeout=60))
                data = json.loads(resp.read().decode("utf-8"))
                results.extend([item["embedding"] for item in data["data"]])
                break
            except urllib.error.HTTPError as e:
                if e.code == 429 and attempt < 2:
                    await asyncio.sleep(2 * (attempt + 1))
                else:
                    raise
        if i + 5 < len(texts):
            await asyncio.sleep(1)

    return np.array(results)


async def init_rag():
    if not os.path.exists(WORKING_DIR):
        os.mkdir(WORKING_DIR)

    rag = LightRAG(
        working_dir=WORKING_DIR,
        llm_model_func=llm_model_func,
        embedding_func=EmbeddingFunc(
            embedding_dim=1024,
            max_token_size=8192,
            func=jina_embed,
        ),
    )
    await rag.initialize_storages()
    return rag


async def test():
    rag = await init_rag()

    # Insert test data
    await rag.ainsert("Пользователя зовут Петя. Он живёт в Москве. Мы говорили про имена, бот предложил имя Макс.")

    # Query
    result = await rag.aquery("Какое имя предлагал бот?", param=QueryParam(mode="naive"))
    print("Query result:", result)

    await rag.finalize_storages()


if __name__ == "__main__":
    asyncio.run(test())
