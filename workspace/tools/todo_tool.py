"""Тул todo: постоянный список задач с видимым в промпте состоянием.

Строки нумеруются ("1. [ ] задача") — по номеру ими управляют (done/remove).
"""

import builtins as _b

TOOL = {
    "name": "todo",
    "description": "Постоянный список задач (чинить/написать/сделать и т.п.).",
    "state": {
        "title": "📝 Список задач",
        "file": "data/todo_state.json",
        "view": "list",
    },
    "methods": {
        "add": {
            "description": "Добавить новую задачу",
            "params": [
                {"name": "task", "type": "str", "required": True,
                 "description": "текст задачи"},
            ],
        },
        "done": {
            "description": "Отметить задачу выполненной по номеру",
            "params": [
                {"name": "index", "type": "int", "required": True,
                 "description": "номер задачи (начиная с 1)"},
            ],
        },
        "remove": {
            "description": "Удалить задачу по номеру",
            "params": [
                {"name": "index", "type": "int", "required": True,
                 "description": "номер задачи (начиная с 1)"},
            ],
        },
        "list": {
            "description": "Показать все задачи",
            "params": [],
        },
    },
}


class State:
    """Хранилище состояния: файл JSON {"lines": [...]}."""

    def __init__(self, rel_path):
        from pathlib import Path
        import json as _j
        self.json = _j
        self.path = Path("/root/tg_bot/workspace") / rel_path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def read(self):
        try:
            return self.json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            return {"lines": []}

    def write(self, data):
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(self.json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def lines(self):
        return self.read().get("lines", [])

    def set_lines(self, lines):
        data = self.read()
        data["lines"] = _b.list(lines)
        self.write(data)


def list():
    return State(TOOL["state"]["file"]).lines()


def add(task):
    st = State(TOOL["state"]["file"])
    n = len(st.lines()) + 1
    st.set_lines(st.lines() + [f"{n}. [ ] {task}"])
    return {"added": task, "total": n}


def done(index):
    st = State(TOOL["state"]["file"])
    lines = st.lines()
    if not (1 <= index <= len(lines)):
        return {"error": f"нет задачи с номером {index}, всего строк: {len(lines)}"}
    item = lines[index - 1]
    if "[x]" in item:
        return {"already": True}
    lines[index - 1] = item.replace("[ ]", "[x]", 1)
    st.set_lines(lines)
    return {"done": index}


def remove(index):
    st = State(TOOL["state"]["file"])
    lines = st.lines()
    if not (1 <= index <= len(lines)):
        return {"error": f"нет задачи с номером {index}, всего строк: {len(lines)}"}
    removed = lines.pop(index - 1)
    # Перенумеровываем оставшиеся строки, чтобы номера были консистентными
    renumbered = []
    for i, line in enumerate(lines, 1):
        rest = line.split(" ", 1)[1] if " " in line else line
        renumbered.append(f"{i}. {rest}")
    st.set_lines(renumbered)
    return {"removed": removed}


def set_raw(lines):
    """Перезаписать строки целиком (для отладки/импорта, lines — список)."""
    st = State(TOOL["state"]["file"])
    st.set_lines(list(lines))
    return {"total": len(lines)}