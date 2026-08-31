"""Файловый тул: чтение, поиск и правки файлов в твоём workspace.

Работает как привычные opencode-инструменты: read / glob / grep / edit / write.
Все пути — отностительность workspace (".", "data/x.json"), за пределы выйти нельзя.
Ошибки метод возвращает через исключение — бот покажет их тебе.
"""

WORKSPACE = "/root/tg_bot/workspace"

from pathlib import Path  # noqa: E402

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}
_MIME_MAP = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
             ".webp": "image/webp", ".gif": "image/gif", ".bmp": "image/bmp"}

TOOL = {
    "name": "file",
    "description": "Файлы в твоём workspace: чтение/директории, поиск, точечные правки.",
    "methods": {
        "read": {
            "description": "Содержимое файла (текст), список файлов в каталоге, или изображение (jpg/png/webp — ты его увидишь)",
            "params": [
                {"name": "path", "type": "str", "required": True, "description": "путь относительно workspace"}
            ],
        },
        "glob": {
            "description": "Найти файлы по маске (напр. '**/*.py') — пути относительно workspace",
            "params": [
                {"name": "pattern", "type": "str", "required": True, "description": "маска, может быть рекурсивной"}
            ],
        },
        "grep": {
            "description": "Найти строки с совпадением в текстовых файлах",
            "params": [
                {"name": "pattern", "type": "str", "required": True, "description": "regex"},
                {"name": "path", "type": "str", "required": False, "default": ".", "description": "файл или каталог"},
            ],
        },
        "edit": {
            "description": "Заменить единственное вхождение old на new в файле (точечная правка)",
            "params": [
                {"name": "path", "type": "str", "required": True, "description": "путь относительно workspace"},
                {"name": "old", "type": "str", "required": True, "description": "точный старый фрагмент"},
                {"name": "new", "type": "str", "required": True, "description": "замена"},
            ],
        },
        "write": {
            "description": "Записать файл целиком (создаст каталоги)",
            "params": [
                {"name": "path", "type": "str", "required": True, "description": "путь относительно workspace"},
                {"name": "content", "type": "str", "required": True, "description": "полное содержимое файла"},
            ],
        },
        "append": {
            "description": "Дописать текст в конец файла (создаст файл/каталоги, если их нет)",
            "params": [
                {"name": "path", "type": "str", "required": True, "description": "путь относительно workspace"},
                {"name": "content", "type": "str", "required": True, "description": "текст для добавления в конец"},
            ],
        },
    },
}


def _path(p):
    full = (Path(WORKSPACE) / p).resolve()
    try:
        full.relative_to(Path(WORKSPACE).resolve())
    except ValueError:
        raise ValueError(f"путь за пределами workspace: {p!r}")
    return full


def read(path):
    """Вернуть содержимое файла или список содержимого каталога."""
    import datetime as _dt

    try:
        full = _path(path)
    except ValueError as e:
        return {"kind": "error", "message": str(e)}
    if not full.exists():
        return {"kind": "error", "message": f"нет такого пути: {path!r}"}
    if full.is_dir():
        out = []
        for item in sorted(full.iterdir()):
            st = item.stat()
            kind = "dir" if item.is_dir() else "file"
            out.append(
                f"{item.name}  [{kind}]  {st.st_size}B  {_dt.datetime.fromtimestamp(st.st_mtime).strftime('%Y-%m-%d %H:%M')}"
            )
        return {"path": path, "kind": "dir", "entries": out}
    if full.is_file() and full.suffix.lower() in IMAGE_EXTS:
        return {
            "path": path,
            "kind": "image",
            "absolute_path": str(full),
            "size": full.stat().st_size,
            "mime": _MIME_MAP.get(full.suffix.lower(), "image/png"),
        }
    try:
        data = full.read_bytes()
    except PermissionError:
        return {"kind": "error", "message": f"нет доступа к файлу: {path!r}"}
    except OSError as e:
        return {"kind": "error", "message": f"ошибка чтения {path!r}: {e}"}
    text = data.decode("utf-8", errors="replace")
    if len(text) > 30000:
        return {"path": path, "kind": "file", "size": len(data),
                "truncated": True, "note": "обрезано на 30000 символов — читай через grep/каталог",
                "content": text[:30000]}
    return {"path": path, "kind": "file", "size": len(data), "content": text}


def glob(pattern):
    """Найти файлы по маске (например '**/*.py'), пути относительно workspace."""
    import glob as _glob

    base = Path(WORKSPACE).resolve()
    matches = sorted(_glob.glob(str(base / pattern), recursive=True))
    return [str(Path(m).relative_to(base)) for m in matches]


def grep(pattern, path="."):
    """Найти строки с совпадением в текстовых файлах: 'путь:строка: текст'."""
    import re as _re

    base = _path(path)
    expr = _re.compile(pattern)
    hits = []
    if base.is_file():
        targets = [base]
    else:
        targets = (t for t in base.rglob("*") if t.is_file()
                   and "__pycache__" not in t.parts and ".git" not in t.parts)
    for f in targets:
        try:
            if f.stat().st_size > 1_000_000:
                continue
            text = f.read_bytes().decode("utf-8", errors="replace")
        except OSError:
            continue
        if "\x00" in text:
            continue
        for i, line in enumerate(text.splitlines(), 1):
            if expr.search(line):
                rel = str(f.relative_to(Path(WORKSPACE).resolve()))
                hits.append(f"{rel}:{i}: {line.strip()}")
                if len(hits) >= 100:
                    return {"count": ">=100", "hits": hits}
    return {"count": len(hits), "hits": hits}


def edit(path, old, new):
    """Заменить единственное вхождение old на new (точечная правка)."""
    full = _path(path)
    if not full.exists():
        raise FileNotFoundError(f"нет такого файла: {path!r}")
    text = full.read_text(encoding="utf-8")
    n = text.count(old)
    if n == 0:
        raise ValueError(f"фрагмент для замены не найден в {path!r} (look: {old[:60]!r})")
    if n > 1:
        raise ValueError(f"фрагмент встречается {n} раз в {path!r} — уточни old (добавь контекст)")
    new_text = text.replace(old, new, 1)
    tmp = full.with_suffix(full.suffix + ".tmp")
    tmp.write_text(new_text, encoding="utf-8")
    tmp.replace(full)
    return {"path": path, "replaced": 1, "hint": old[:40]}


def write(path, content):
    """Записать файл целиком (создаст каталоги). Вернуть размер."""
    full = _path(path)
    full.parent.mkdir(parents=True, exist_ok=True)
    tmp = full.with_suffix(full.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(full)
    return {"path": path, "bytes": len(content.encode("utf-8"))}


def append(path, content):
    """Дописать текст в конец файла (создаст файл/каталоги, если их нет)."""
    full = _path(path)
    full.parent.mkdir(parents=True, exist_ok=True)
    with full.open("a", encoding="utf-8") as f:
        f.write(content)
    return {"path": path, "bytes": len(content.encode("utf-8"))}