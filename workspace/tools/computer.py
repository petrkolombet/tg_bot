"""Тул computer: удалённое управление AI-агентом на Windows ПК.

Работает через FastAPI-сервер (server.py), запущенный на Windows.
Сервер поднимает os-ai-computer-use и предоставляет эндпоинты для управления.
Штурман (бот) ставит задачи, проверяет скриншоты, останавливает агента.
"""

import json
import os
import urllib.request
import urllib.error
from pathlib import Path

import providers

WORKSPACE = "/root/tg_bot/workspace"
SCREENSHOT_PATH = os.path.join(WORKSPACE, "current_screen.png")
SERVER_URL_FILE = os.path.join(WORKSPACE, "computer_url.txt")


def _server_url():
    """URL Windows-сервера: приоритет — env, потом файл computer_url.txt, потом дефолт.
    Туннельный URL меняется при каждом рестарте cloudflared, поэтому держим его в файле."""
    env = os.environ.get("COMPUTER_SERVER_URL")
    if env:
        return env.rstrip("/")
    try:
        url = Path(SERVER_URL_FILE).read_text(encoding="utf-8").strip()
        if url:
            return url.rstrip("/")
    except Exception:
        pass
    return "http://localhost:8000"

TOOL = {
    "name": "computer",
    "description": "Удалённое управление AI-агентом на Windows ПК: постановка задач, проверка скриншотов, контроль выполнения.",
    "methods": {
        "connect": {
            "description": "Проверить доступность Windows-сервера",
            "params": [],
        },
        "run_task": {
            "description": "Отправить задачу агенту на Windows ПК",
            "params": [
                {"name": "task", "type": "str", "required": True,
                 "description": "текст задачи для выполнения на ПК"},
                {"name": "model", "type": "str", "required": False, "default": "",
                 "description": "модель LLM для агента (если пусто — использует серверную)"},
            ],
        },
        "stop": {
            "description": "Принудительно остановить текущего агента",
            "params": [],
        },
        "get_status": {
            "description": "Получить текущий статус агента и последние логи",
            "params": [],
        },
        "get_screen": {
            "description": "Получить последний скриншот экрана с Windows ПК",
            "params": [],
        },
    },
}


def _request(method, path, data=None, timeout=15):
    """Универсальный HTTP-запрос к Windows-серверу."""
    url = f"{_server_url()}{path}"
    body = json.dumps(data).encode("utf-8") if data else None
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"} if body else {},
        method=method,
    )
    try:
        resp = providers.open_url(req, timeout=timeout)
        return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body_text = e.read().decode("utf-8", errors="replace")
        try:
            return json.loads(body_text)
        except ValueError:
            return {"error": f"HTTP {e.code}: {body_text[:200]}"}
    except urllib.error.URLError as e:
        return {"error": f"Сервер недоступен: {e.reason}"}
    except Exception as e:
        return {"error": str(e)}


def connect():
    """Проверить доступность сервера на Windows ПК."""
    result = _request("GET", "/status")
    if "error" in result:
        return {"status": "offline", "error": result["error"]}
    return {
        "status": "online",
        "agent_status": result.get("status", "unknown"),
        "message": result.get("message", ""),
    }


def run_task(task, model=""):
    """Отправить задачу агенту на Windows ПК."""
    payload = {"task": task}
    llm_url = os.environ.get("COMPUTER_LLM_URL", "")
    llm_key = os.environ.get("COMPUTER_LLM_API_KEY", "")
    llm_model = model or os.environ.get("COMPUTER_LLM_MODEL", "")
    llm_proxy = os.environ.get("COMPUTER_LLM_PROXY", "")
    if llm_url:
        payload["base_url"] = llm_url
    if llm_key:
        payload["api_key"] = llm_key
    if llm_model:
        payload["model"] = llm_model
    if llm_proxy:
        parts = llm_proxy.split(":")
        if len(parts) == 4:
            h, p, u, pw = parts
            llm_proxy = f"http://{u}:{pw}@{h}:{p}"
        elif not llm_proxy.startswith("http"):
            llm_proxy = f"http://{llm_proxy}"
        payload["proxy"] = llm_proxy
    result = _request("POST", "/start", data=payload, timeout=10)
    if "error" in result:
        return {"error": result["error"]}
    return {"status": "started", "task": task, "message": result.get("message", "")}


def stop():
    """Принудительно остановить текущего агента."""
    result = _request("POST", "/stop", timeout=10)
    if "error" in result:
        return {"error": result["error"]}
    return {"status": "stopped", "message": result.get("message", "")}


def get_status():
    """Получить текущий статус агента и последние логи."""
    result = _request("GET", "/status")
    if "error" in result:
        return {"error": result["error"]}
    return {
        "status": result.get("status", "unknown"),
        "message": result.get("message", ""),
        "logs": result.get("logs", []),
    }


def get_screen():
    """Получить последний скриншот экрана с Windows ПК.
    Сохраняет PNG в workspace/current_screen.png для чтения через file.read."""
    result = _request("GET", "/screen", timeout=15)
    if "error" in result:
        return {"error": result["error"]}

    image_data = result.get("image")
    if not image_data:
        return {"error": "Сервер не вернул скриншот"}

    # Если пришёл base64 — декодируем
    import base64
    try:
        img_bytes = base64.b64decode(image_data)
    except Exception:
        # Если не base64 — возможно, это уже байты (не должно быть, но на всякий)
        return {"error": "Не удалось декодировать скриншот"}

    # Сохраняем в workspace
    with open(SCREENSHOT_PATH, "wb") as f:
        f.write(img_bytes)

    return {
        "status": "saved",
        "path": "current_screen.png",
        "absolute_path": SCREENSHOT_PATH,
        "size": len(img_bytes),
        "hint": "Используй file.read(path='current_screen.png') чтобы увидеть скриншот",
    }
