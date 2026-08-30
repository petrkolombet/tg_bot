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
import json
import logging
import os
import re
from pathlib import Path

logger = logging.getLogger(__name__)

WORKSPACE_DIR = Path("/root/tg_bot/workspace")
TOOLS_DIR = WORKSPACE_DIR / "tools"

# Кеш: имя_тула -> {"mtime": float, "manifest": dict}
_REGISTRY = {}
_REGISTRY_LOADED = False

# Кеш состояний: (абс.путь_state_file) -> {"mtime": float, "lines": list[str]}
_STATE_CACHE = {}

# Дефолт для лимита строк состояния, если не задан в манифесте
_STATE_DEFAULT_LIMIT = 8

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
        "description": "Поисковый ИИ-агент: ищет в интернете, возвращает свежие детали и ссылки.",
        "methods": {
            "run": {
                "description": "Найти в интернете",
                "params": [
                    {"name": "query", "type": "str", "required": True, "description": "запрос — полной фразой, как к поисковику (не ключевики)"},
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
    """Вытаскивает литеральный dict TOOL из исходника через AST.

    Модель часто выносит значения в переменные (STATE_FILE = "...").
    Чтобы не ломаться на этом, собираем верхнеуровневые простые константы
    (NAME = 'str' | int | float | bool | list | dict из литералов) и при
    разборе TOOL подставляем их как значения."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return None

    consts = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    try:
                        consts[target.id] = ast.literal_eval(node.value)
                    except (ValueError, TypeError):
                        pass

    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "TOOL":
                    val = _eval_with_consts(node.value, consts)
                    if isinstance(val, dict) and val.get("name") and isinstance(val.get("methods"), dict):
                        return val
    return None


def _eval_with_consts(node, consts):
    """Рекурсивный разбор литерала с подстановкой имён из consts."""
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        return consts.get(node.id)
    if isinstance(node, ast.Str):  # py<3.8 совместимость
        return node.s
    if isinstance(node, ast.Num):
        return node.n
    if isinstance(node, ast.List):
        return [_eval_with_consts(e, consts) for e in node.elts]
    if isinstance(node, ast.Tuple):
        return tuple(_eval_with_consts(e, consts) for e in node.elts)
    if isinstance(node, ast.Set):
        return {_eval_with_consts(e, consts) for e in node.elts}
    if isinstance(node, ast.Dict):
        d = {}
        for k, v in zip(node.keys, node.values):
            key = _eval_with_consts(k, consts)
            if key is not None:
                d[key] = _eval_with_consts(v, consts)
        return d
    if isinstance(node, (ast.UnaryOp,)) and isinstance(node.op, (ast.USub, ast.UAdd)):
        operand = _eval_with_consts(node.operand, consts)
        if isinstance(operand, (int, float)):
            return -operand if isinstance(node.op, ast.USub) else operand
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _eval_with_consts(node.left, consts)
        right = _eval_with_consts(node.right, consts)
        if isinstance(left, str) and isinstance(right, str):
            return left + right
        if isinstance(left, (int, float)) and isinstance(right, (int, float)):
            return left + right
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


def _read_state(manifest: dict):
    """Читает state_file тула -> dict(title, lines, limit) или None.

    Тул сам готовит строки для показа (`lines`) и пишет их в state_file.
    Реестр ничего не интерпретирует — только показывает готовые строки.
    Кеш по mtime файла состояния: перечитываем только когда тул его поменял.
    """
    state = manifest.get("state")
    if not state or not isinstance(state, dict):
        return None
    rel = state.get("file")
    if not rel:
        return None
    path = (WORKSPACE_DIR / rel).resolve()
    # Безопасность: состояние может лежать только внутри workspace
    try:
        path.relative_to(WORKSPACE_DIR.resolve())
    except ValueError:
        logger.warning("state_file вне workspace: %s", path)
        return None
    if not path.exists():
        return None
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None

    cached = _STATE_CACHE.get(str(path))
    if cached and cached["mtime"] == mtime:
        lines = cached["lines"]
    else:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        lines = data.get("lines") if isinstance(data, dict) else None
        if not isinstance(lines, list):
            return None
        lines = [str(x) for x in lines]
        _STATE_CACHE[str(path)] = {"mtime": mtime, "lines": lines}

    return {
        "title": state.get("title") or f"{manifest.get('name', 'тул')}",
        "lines": lines,
        "limit": int(state.get("limit") or _STATE_DEFAULT_LIMIT),
    }


def _state_block(manifest: dict) -> str:
    """Блок <STATE:title>...</STATE> для промпта из state_file тула."""
    st = _read_state(manifest)
    if not st:
        return ""
    lines = st["lines"]
    limit = max(1, st["limit"])
    shown = lines[:limit]
    out = [f"<STATE:{st['title']}>"]
    out.extend(shown)
    rest = len(lines) - len(shown)
    if rest > 0:
        view = manifest.get("state", {}).get("view")
        if view:
            out.append(f"(+{rest} ещё — полное состояние через {manifest['name']}.{view})")
        else:
            out.append(f"(+{rest} ещё — не показано)")
    out.append("</STATE>")
    return "\n".join(out)


def build_tools_block() -> str:
    """Блок <TOOLS> + блоки <STATE> состояниев для промпта."""
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

    for manifest in get_custom_tools().values():
        st = _state_block(manifest)
        if st:
            lines.append("")
            lines.append(st)
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


_ESCAPE_MAP = {
    "n": "\n", "t": "\t", "r": "\r",
    "\\": "\\", "'": "'", '"': '"', "0": "\0",
}


def _unescape_literals(s: str) -> str:
    """Раскрывает escape-последовательности внутри строкового аргумента tool_call.

    Модель копирует фрагменты из вывода file.read, где настоящие переносы строк
    показаны JSON-экранированием ('\n' — два символа). Без раскрытия edit/write
    не находили old и портили файлы, записывая литеральный backslash-n."""
    if "\\" not in s:
        return s
    out = []
    i = 0
    n = len(s)
    while i < n:
        c = s[i]
        if c == "\\" and i + 1 < n:
            nxt = s[i + 1]
            if nxt in _ESCAPE_MAP:
                out.append(_ESCAPE_MAP[nxt])
                i += 2
                continue
        out.append(c)
        i += 1
    return "".join(out)


def _parse_value(v: str):
    if not v:
        return ""
    if (v[0] == '"' and v[-1] == '"') or (v[0] == "'" and v[-1] == "'"):
        return _unescape_literals(v[1:-1])
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