"""Кастомный инструмент: отправка файла в Telegram.

Формат кастомного тула:
- литеральный манифест TOOL (name, description, methods с params);
- метод = python-функция, принимающая аргументы по имени;
- консольный запуск: python3 send_file.py send --path <file> -> печатает JSON результат.
"""

import os
import sys
import json
import asyncio

TOOL = {
    "name": "send_file",
    "description": "Отправить файл (из workspace) владельцу в Telegram.",
    "methods": {
        "send": {
            "description": "Отправить файл в Telegram",
            "params": [
                {"name": "path", "type": "str", "required": True,
                 "description": "путь к файлу (относительно workspace или абсолютный)"}
            ],
        }
    },
}

CHAT_ID = os.getenv('ALLOWED_USER_ID', '')


async def _send(path):
    from aiogram import Bot
    from aiogram.types import FSInputFile
    token = os.getenv('TELEGRAM_TOKEN', '')
    if not token:
        return {"ok": False, "error": "TELEGRAM_TOKEN не найден"}
    if not CHAT_ID:
        return {"ok": False, "error": "ALLOWED_USER_ID не найден"}
    if not os.path.exists(path):
        return {"ok": False, "error": f"файл не найден: {path}"}

    bot = Bot(token=token)
    try:
        doc = FSInputFile(path)
        await bot.send_document(chat_id=int(CHAT_ID), document=doc)
        return {"ok": True, "sent": os.path.basename(path)}
    except Exception as e:
        return {"ok": False, "error": f"ошибка отправки: {e}"}
    finally:
        await bot.session.close()


def send(path):
    """Синхронная обёртка (почта-стиль): вызывает async и возвращает dict."""
    return asyncio.run(_send(path))


def _main():
    method = sys.argv[1] if len(sys.argv) > 1 else None
    if method != "send":
        print(json.dumps({"ok": False, "error": "укажи метод: send"}, ensure_ascii=False))
        sys.exit(1)
    argv = sys.argv[2:]
    path = None
    if "--path" in argv:
        idx = argv.index("--path")
        if idx + 1 < len(argv):
            path = argv[idx + 1]
    if not path:
        print(json.dumps({"ok": False, "error": "обязательный аргумент --path"}, ensure_ascii=False))
        sys.exit(1)
    try:
        print(json.dumps({"ok": True, "result": send(path)}, ensure_ascii=False, default=str))
    except Exception as e:
        print(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False))


if __name__ == '__main__':
    _main()