"""Браузерный тул: открытие сайтов, клики, заполнение форм через демон-браузер.

Тонкий клиент HTTP-демона на 127.0.0.1:18933 (browser_daemon.py, systemd tg-browser).
Сам демон держит headless chromium: одна вкладка, автокуки, idle-закрытие.
Этот файл — только манифест и прокси-методы, браузер ВНЕ этого процесса.
"""

import json
import urllib.request

import providers

DAEMON = "http://127.0.0.1:18933"

TOOL = {
    "name": "browser",
    "description": "Браузер — ТОЛЬКО для ДЕЙСТВИЙ: регистрация, отклик на вакансию, заявка, логин, клики, заполнение форм. НЕ для чтения текста, новостей, поиска информации или контента — для этого есть поисковой агент. Подписки/продления и иной текст тоже НЕ читать. Возвращает только заголовок страницы и кликабельные элементы.",
    "methods": {
        "open": {
            "description": "Открыть URL (одна вкладка — прежняя закрывается). Если URL не указан — откроет последнюю посещённую страницу. Возвращает заголовок + кликабельные элементы (не контент).",
            "params": [
                {"name": "url", "type": "str", "required": False, "description": "https:// ссылка. Пусто = вернуться на последний посещённый URL"},
                {"name": "profile", "type": "str", "required": False, "default": "main", "description": "имя профиля с куками (сессии различных сайтов не смешивать)"},
            ],
        },
        "snapshot": {
            "description": "Обновить состояние страницы: заголовок + кликабельные элементы (ref). Зови если страница могла измениться без действий браузера. Без контента — текст только через dump() при реальной необходимости.",
            "params": [
                {"name": "include_text", "type": "bool", "required": False, "default": False, "description": "включить текст страницы (ТОЛЬКО если это реально нужно для действия)"},
            ],
        },
        "click": {
            "description": "Кликнуть элемент по ref из последнего snapshot.",
            "params": [
                {"name": "ref", "type": "str", "required": True, "description": "например e3"},
            ],
        },
        "fill": {
            "description": "Заполнить поле (input/textarea) по ref. После — новый snapshot.",
            "params": [
                {"name": "ref", "type": "str", "required": True, "description": "ref поля"},
                {"name": "value", "type": "str", "required": True, "description": "текст для ввода"},
            ],
        },
        "submit": {
            "description": "Нажать Enter (отправить активную форму). После — новый snapshot.",
            "params": [],
        },
        "reload": {
            "description": "Обновить текущую страницу. После — новый snapshot.",
            "params": [],
        },
        "dump": {
            "description": "Полный текст страницы. Вызывай ТОЛЬКО если для действия реально нужен контент (например, ищешь слово/кнопку, не попавшую в паспорт). Не вызывай из любопытства — браузер не для чтения.",
            "params": [
                {"name": "limit", "type": "int", "required": False, "default": 3000, "description": "макс. символов"},
            ],
        },
        "screenshot": {
            "description": "Скриншот текущей страницы, сохраняет в workspace/browser_shots/, возвращает путь.",
            "params": [],
        },
        "status": {
            "description": "Статус браузера: запущен ли, текущий URL.",
            "params": [],
        },
        "close": {
            "description": "Закрыть браузер и освободить память (куки сохранены).",
            "params": [],
        },
        "reset_state": {
            "description": "Стереть сохранённые куки профиля (например, перед повторной регистрацией).",
            "params": [
                {"name": "profile", "type": "str", "required": False, "default": "main", "description": "имя профиля"},
            ],
        },
    },
}


def _call(action, **kwargs):
    payload = json.dumps({"action": action, **kwargs}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(DAEMON, data=payload, method="POST")
    try:
        with providers.open_url(req, timeout=70) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as e:
        raise RuntimeError(f"демон браузера недоступен ({e}). Попроси владельца запустить tg-browser.service") from e
    if not data.get("ok"):
        raise RuntimeError(data.get("error", "ошибка браузера"))
    return data.get("result")


def open(url, profile="main"):
    """Открыть URL. Возвращает заголовки + кликабельные элементы."""
    return _call("open", url=url, profile=profile)


def snapshot(include_text=False):
    """Обновить состояние страницы: заголовки + элементы (а не текст)."""
    return _call("snapshot", include_text=bool(include_text))


def click(ref):
    """Клик по ref. Возвращает новый snapshot."""
    return _call("click", ref=ref)


def fill(ref, value):
    """Заполнить поле по ref. Возвращает новый snapshot."""
    return _call("fill", ref=ref, value=value)


def submit():
    """Enter. Возвращает новый snapshot."""
    return _call("submit")


def reload():
    """Обновить страницу. Возвращает новый snapshot."""
    return _call("reload")


def dump(limit=3000):
    """Полный текст страницы (по умолчанию до 3000 символов)."""
    return _call("dump", limit=int(limit))


def screenshot():
    """Скриншот, возвращает путь в workspace."""
    return _call("screenshot")


def status():
    """Статус браузера."""
    return _call("status")


def close():
    """Закрыть браузер (куки сохранены)."""
    return _call("close")


def reset_state(profile="main"):
    """Стереть куки профиля."""
    return _call("reset_state", profile=profile)