"""Инструмент для выполнения PowerShell-команд на ПК через cmd_server."""

import urllib.request
import json

TOOL = {
    "name": "cmd_pc",
    "description": "Выполнение PowerShell-команд и скриптов на удалённом ПК пользователя через cmd_server.",
    "methods": {
        "run": {
            "description": "Выполнить PowerShell команду на ПК",
            "params": [
                {"name": "cmd", "type": "str", "required": True, "description": "PowerShell команда для выполнения"}
            ]
        }
    }
}

def run(cmd):
    url = "[http://petrkolombet.ru/run](http://petrkolombet.ru/run)"
    payload = json.dumps({"cmd": cmd}).encode("utf-8")
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as response:
            return json.loads(response.read().decode("utf-8"))
    except Exception as e:
        return {"error": str(e)}
