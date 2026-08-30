"""Универсальный runner для кастомных тулов.

Тулам НЕ нужно писать _main / argparse / if __name__ — всё делает runner:
1. импортирует файл тула как модуль;
2. вызывает метод по имени с kwargs (JSON из stdin);
3. печатает {"ok": true, "result": ...} или {"ok": false, "error": ...}.

Метод тула — обычная функция, ВОЗВРАЩАЕТ результат. Если метод (ошибочно)
сам печатает в stdout и возвращает None — его вывод подхватывается как result,
чтобы система работала даже при неточном следовании контракту.
"""

import contextlib
import importlib.util
import io
import json
import sys
from pathlib import Path


def _load_result(func, kwargs):
    """Вызывает метод. Если метод вернул None, но сам напечатал в stdout —
    его вывод считается результатом. Печать, являющаяся готовым JSON с полем
    'ok'/'result', отдаётся как есть (не оборачивается повторно)."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        result = func(**kwargs)
    printed = buf.getvalue().strip()
    if result is None and printed:
        try:
            parsed = json.loads(printed)
        except ValueError:
            return printed
        if isinstance(parsed, dict) and ("ok" in parsed or "result" in parsed):
            return parsed
        return parsed
    return result


def _finish(result):
    if (
        isinstance(result, dict)
        and "ok" in result
        and "result" in result
        and result.get("ok") is not None
    ):
        print(json.dumps(result, ensure_ascii=False, default=str))
    else:
        print(json.dumps({"ok": True, "result": result}, ensure_ascii=False, default=str))


def main():
    script_path = sys.argv[1]
    method = sys.argv[2]
    kwargs_raw = sys.stdin.read() or "{}"
    try:
        kwargs = json.loads(kwargs_raw)
    except ValueError:
        kwargs = {}

    tools_dir = str(Path(script_path).resolve().parent)
    sys.path.insert(0, tools_dir)

    spec = importlib.util.spec_from_file_location("tool_mod", script_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    func = getattr(mod, method, None)
    if func is None or not callable(func):
        raise AttributeError(f"в туле {Path(script_path).name} нет метода {method!r}")

    result = _load_result(func, kwargs)
    _finish(result)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:
        print(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False))