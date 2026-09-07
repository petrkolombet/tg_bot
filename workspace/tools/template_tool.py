"""ШАБЛОН кастомного инструмента (template_tool).

Скопируй этот файл, переименуй (например my_tool.py), заполни TOOL и методы.

ОБЯЗАТЕЛЬНО (иначе тул не заработает):
1. TOOL — словарь из ТОЛЬКО констант-литералов (строки/числа/списки/словари).
   НЕЛЬЗЯ переменные, вызовы функций, f-строки внутри TOOL.
2. name — короткое имя тула (как вызывается в tool_call).
3. description — крупным планом, что делает тул.
4. methods.<метод>.params — список параметров:
   name(имя), type(str/int/bool), required, default, description.
   Имя параметра метода = name аргумента функции.
5. Каждый метод — функция, ВОЗВРАЩАЕТ результат (никогда не print в методе).
6. НЕ писать _main / argparse / if __name__ — это сделают за тебя.
7. УПРАВЛЕНИЕ СТРОКАМИ (когда это уместно): если строки состояния будут
   менять/отмечать/удалять по номеру или id — ЗАРАНЕЕ продумай и нарисуй в
   строках явный идентификатор, чтобы управлять ими однозначно. Вид — на твоё
   усмотрение: либо настоящий id ("Ab3f: [ ] дело"), либо порядковый номер
   ("1. [ ] дело", "2. [x] дело"). Значение index/id = видимый в строке
   идентификатор. Не оставляй строки без адреса — управлять ими будет нечем,
   и при удалении ты будешь угадывать номер от балды. Примеры в этом файле ниже.

СОСТОЯНИЕ (state) — если тебе нужно видимое в промпте состояние:
"state": {"title": "...", "file": "data/todo.json", "view": "метод"}
- file — путь ОТНОСИТЕЛЬНО workspace (data/todo.json); только внутри workspace.
- Состояние пишет сам тул (класс State: add_line / set_lines / lines).
- Система сама читает "lines" из файла и показывает тебе их в промпте —
  рендер делать не надо.
- title — заголовок блока <STATE:title>; view — метод, показывающий полное состояние.

ВЫЗОВ: ты пишешь "tool_call": "имя.метод(арг=знач)". Результат — JSON в stdout.
"""

TOOL = {
    "name": "example",
    "description": "Пример тула: что делает и зачем.",
    "state": {
        "title": "🗒 Пример",
        "file": "data/example_state.json",
        "view": "list",
    },
    "methods": {
        "hello": {
            "description": "Что делает метод",
            "params": [
                {"name": "name", "type": "str", "required": True,
                 "description": "имя, к которому обратиться"},
                {"name": "count", "type": "int", "required": False, "default": 1,
                 "description": "сколько раз повторить"},
            ],
        },
        "list": {
            "description": "Полный список состояния",
            "params": [],
        },
        "add": {
            "description": "Добавить строку в состояние",
            "params": [
                {"name": "text", "type": "str", "required": True,
                 "description": "текст строки"},
            ],
        },
    },
}


class State:
    """Хранилище состояния: файл JSON {"lines": [...], "meta": {...}}.
    Ты пишешь данные — система показывает их тебе в промпте."""

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
        data["lines"] = list(lines)
        self.write(data)

    def add_line(self, text):
        data = self.read()
        data["lines"].append(text)
        self.write(data)
        return len(data["lines"])


def hello(name, count=1):
    return {"say": f"Привет, {name}!" * count}


def list():
    """Состояние с номерами — их видишь ты, по ним управляешь строками."""
    return State(TOOL["state"]["file"]).lines()


def add(text):
    st = State(TOOL["state"]["file"])
    num = st.add_line(f"{len(st.lines()) + 1}. [ ] {text}")
    return {"added": text, "total": num}