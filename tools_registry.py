"""Реестр инструментов: авторегистрация тулов из workspace/tools/.

Концепция:
- Каждый тул = отдельный .py в workspace/tools/ с литеральным манифестом `TOOL = {...}`
  (имя, описание, методы с параметрами).
- Манифесты читаются через AST (код тула в процессе бота НЕ исполняется) → hot reload
  по mtime без перезапуска.
- Встроенные тулы (shell/search/memory) — обрабатываются напрямую в _tool_loop,
  здесь только их каталог-описание для промпта.
- Кастомные тулы вызывают через server_access.execute_custom_tool (тот же sandbox).
- tool_call — строка-вызов: "имя.метод(арг=знач, арг2='знач2')".
"""

import ast
import logging
import os
import re
from pathlib import Path

logger = logging.getLogger(__name__)

TOOLS_DIR = Path("/root/tg_bot/workspace/tools")

# Кеш: имя_тула -> {"mtime": float, "manifest": dict}
_REGISTRY = {}
_REGISTRY_LOADED = False

# Встроенные тулы — только описание для каталога; исполнение в _tool_loop
BUILTIN_TOOLS = {
    "shell": {
        "name": "shell",
        "description": "Выполнение команды в sandbox (рабочая папка /root/tg_bot/workspace).",
        "methods": {
            "run": {
                "description": "Выполнить shell-команду",
                "params": [
                    {"name": "cmd", "type": "str", "required": True, "description": "команда"},
                    {"name": "desc", "type": "str", "required": True, "description": "зачем выполняешь"},
                ],
            }
        },
    },
    "search": {
        "name": "search",
        "description": "Веб-поиск по запросу (через DeepSeek-агента).",
        "methods": {
            "run": {
                "description": "Поиск в интернете",
                "params": [
                    {"name": "query", "type": "str", "required": True, "description": "поисковый запрос"},
                ],
            }
        },
    },
    "memory": {
        "name": "memory",
        "description": "Поиск в собственной памяти (RAG).",
        "methods": {
            "query": {
                "description": "Вспомнить по теме из памяти",
                "params": [
                    {"name": "topic", "type": "str", "required": True, "description": "суть запроса"},
                ],
            }
        },
    },
}

BUILTIN_NAMES = set(BUILTIN_TOOLS)

_CALL_RE = re.compile(r'^\s*([A-Za-z_][\w]*)\.([A-Za-z_][\w]*)\s*\((.*)\)\s*$', re.DOTALL)


def _extract_manifest(text: str):
    """Вытаскивает литеральный dict TOOL из исходника через AST."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return None
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "TOOL":
                    try:
                        val = ast.literal_eval(node.value)
                    except (ValueError, TypeError):
                        return None
                    if isinstance(val, dict) and val.get("name") and isinstance(val.get("methods"), dict):
                        return val
    return None


def _load_file(path: Path):
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    return _extract_manifest(text)


def scan():
    """Сканирует workspace/tools/*.py, обновляет реестр по mtime. Возвращает dict."""
    global _REGISTRY, _REGISTRY_LOADED
    if not TOOLS_DIR.exists():
        return _REGISTRY
    for path in sorted(TOOLS_DIR.glob("*.py")):
        if path.stem.startswith("template_") or path.stem.startswith("example_"):
            _REGISTRY.pop(path.stem, None)
            continue
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        name = path.stem
        cached = _REGISTRY.get(name)
        if cached and cached.get("mtime") == mtime and _REGISTRY_LOADED:
            continue
        manifest = _load_file(path)
        if manifest and isinstance(manifest, dict):
            manifest["file"] = str(path)
            tool_name = manifest.get("name") or name
            _REGISTRY[tool_name] = {"mtime": mtime, "manifest": manifest}
        else:
            _REGISTRY.pop(name, None)
    _REGISTRY_LOADED = True
    return _REGISTRY


def get_tool_manifest(name: str):
    """Манифест тула (встроенного или кастомного) или None."""
    if name in BUILTIN_TOOLS:
        return BUILTIN_TOOLS[name]
    info = scan().get(name)
    return info["manifest"] if info else None


def get_custom_tools():
    """Все кастомные тулы: {name: manifest}."""
    reg = scan()
    return {n: info["manifest"] for n, info in reg.items()}


def build_tools_block() -> str:
    """Лаконичный блок <TOOLS> для промпта. Пересобирается при изменении реестра."""
    tools = list(BUILTIN_TOOLS.values()) + list(get_custom_tools().values())
    if not tools:
        return ""
    lines = [
        "<TOOLS>",
        "Доступные инструменты. Вызывай через поле \"tool_call\" (см. JSON_OUTPUT): \"имя.метод(арг=знач)\". Вот каталог:",
    ]
    for t in tools:
        lines.append(f"- {t['name']} — {t.get('description', '').strip()}")
        for mname, m in t["methods"].items():
            params = []
            for p in m.get("params", []):
                ps = f"{p['name']}:{p['type']}"
                if p.get("required") is False and "default" in p:
                    ps += f"={p['default']}"
                elif p.get("required") is False:
                    ps += "?"
                params.append(ps)
            sig = f"{t['name']}.{mname}({', '.join(params)})"
            lines.append(f"    {sig} — {m.get('description', '').strip()}")
    lines.append("</TOOLS>")
    return "\n".join(lines)


def parse_tool_call(call_str: str):
    """Разбирает 'email.get_unread(limit=5)' -> (tool, method, kwargs).
    Имя/метод валидируются по реестру. Поддерживает JSON-подобные литералы в значениях."""
    m = _CALL_RE.match(call_str or "")
    if not m:
        raise ValueError(f"tool_call не похож на вызов функции: {call_str!r}")
    tool, method, args_s = m.group(1), m.group(2), m.group(3)

    manifest = get_tool_manifest(tool)
    if not manifest:
        raise ValueError(f"неизвестный инструмент: {tool}")
    if method not in manifest.get("methods", {}):
        raise ValueError(f"у инструмента {tool} нет метода {method}")

    kwargs = {}
    if args_s.strip():
        for chunk in _split_top_level(args_s):
            if "=" not in chunk:
                raise ValueError(f"аргумент без имени в вызове {call_str!r}: {chunk!r}")
            k, v = chunk.split("=", 1)
            kwargs[k.strip()] = _parse_value(v.strip())

    schema = manifest["methods"][method]
    for p in schema.get("params", []):
        if p.get("required") and p["name"] not in kwargs:
            raise ValueError(f"не хватает обязательного аргумента {p['name']} для {tool}.{method}")

    return tool, method, kwargs


def _split_top_level(s: str) -> list:
    """Разбивает строку аргументов по запятым вне кавычек/скобок."""
    parts = []
    buf = ""
    quote = None
    depth = 0
    for ch in s:
        if quote:
            buf += ch
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
            buf += ch
        elif ch in "([{":
            depth += 1
            buf += ch
        elif ch in ")]}":
            depth -= 1
            buf += ch
        elif ch == "," and depth == 0:
            parts.append(buf.strip())
            buf = ""
        else:
            buf += ch
    if buf.strip():
        parts.append(buf.strip())
    return parts


def _parse_value(v: str):
    if not v:
        return ""
    if (v[0] == '"' and v[-1] == '"') or (v[0] == "'" and v[-1] == "'"):
        return v[1:-1]
    if v.lower() in ("true", "false", "null", "none"):
        return {"true": True, "false": False, "null": None, "none": None}[v.lower()]
    try:
        return int(v)
    except ValueError:
        pass
    try:
        return float(v)
    except ValueError:
        pass
    return v


def build_cli_args(method_schema: dict, kwargs: dict) -> list:
    """Собирает CLI-аргументы ['--limit', '5'] для кастомного тула по схеме метода."""
    args = []
    params = method_schema.get("params", [])
    for p in params:
        pname = p["name"]
        if pname in kwargs:
            val = kwargs[pname]
            args.append(f"--{pname}")
            args.append(str(val).lower() if isinstance(val, bool) else str(val))
        elif p.get("required") is False and "default" in p:
            args.append(f"--{pname}")
            args.append(str(p["default"]))
    return args