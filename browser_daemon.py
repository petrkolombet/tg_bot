#!/usr/bin/env python3
"""HTTP-демон браузера для агента TG-бота.

Держит один экземпляр headless Chromium (playwright) с одной вкладкой.
HTTP API на 127.0.0.1:18933, JSON POST: {"action": "...", ...args}.

Гарантии:
- одна вкладка: новая навигация закрывает старую page;
- автосохранение куки/сессий -> workspace/browser_state/<profile>.json;
- idle-закрытие браузера (освобождает RAM);
- таймауты навигации и действий.

ВАЖНО: все операции с браузером выполняются в ЕДИНОМ worker-потоке
(playwright sync_api привязан к потоку создания объектов). HTTP-хендлеры
только ставят задачу в очередь и ждут результат.
"""

import json
import logging
import os
import queue
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import config
logger = logging.getLogger("browser_daemon")

WORKSPACE = config.BASE_DIR / "workspace"
STATE_DIR = WORKSPACE / "browser_state"
SHOTS_DIR = WORKSPACE / "browser_shots"
# Постоянные папки браузерных профилей (launch_persistent_context).
# Куки + IndexedDB + localStorage + ServiceWorkers живут в этих папках и
# переживают перезапуск демона (в отличие от storage_state, который переносит
# только cookies+localStorage и теряет остальное состояние браузера).
PROFILE_DIR = STATE_DIR / "profiles"
HOST, PORT = "127.0.0.1", 18933

NAV_TIMEOUT = int(os.environ.get("BROWSER_NAV_TIMEOUT_MS", "50000"))
ACTION_TIMEOUT = int(os.environ.get("BROWSER_ACTION_TIMEOUT_MS", "15000"))
IDLE_CLOSE_SECONDS = int(os.environ.get("BROWSER_IDLE_SECONDS", "300"))
IDLE_CHECK_INTERVAL = 10
REQUEST_TIMEOUT = int(os.environ.get("BROWSER_REQUEST_TIMEOUT", "65"))

SELECTOR = (
    "button, a[href], input, select, textarea, "
    "[role='button'], [role='link'], [role='textbox'], [role='checkbox'], "
    "[role='radio'], [role='combobox'], [role='option'], [role='tab'], "
    "input[type='submit'], input[type='button'], "
    "[class*='btn' i], [class*='button' i]"
)

# Внедряется в каждую страницу контекста: новое "окно" не создаётся,
# target="_blank" и window.open навигируют текущую вкладку.
INIT_SCRIPT = r"""
(() => {
  if (window.__tg_browser_hooked) return;
  window.__tg_browser_hooked = true;

  document.addEventListener('click', (e) => {
    const el = e.target && e.target.closest
      ? e.target.closest('a[target], area[target]')
      : null;
    if (!el) return;
    const t = (el.getAttribute('target') || '').trim().toLowerCase();
    if (t === '_self' || t === '') return;
    const href = el.getAttribute('href');
    if (!href || href.startsWith('#') || href.startsWith('javascript:')) return;
    e.preventDefault();
    e.stopPropagation();
    window.location.href = new URL(href, location.href).href;
  }, true);

  const origOpen = window.open.bind(window);
  window.open = function(url, name, features) {
    if (url && typeof url === 'string') {
      try {
        const abs = new URL(url, location.href).href;
        window.location.href = abs;
        return null;
      } catch (err) {}
    }
    return origOpen(url, name, features);
  };
})();
"""

_WORKER_QUEUE = queue.Queue()


def run_in_browser_thread(fn, timeout=REQUEST_TIMEOUT):
    """Выполнить fn в worker-потоке браузера и дождаться результата."""
    done = threading.Event()
    holder = {"done": done}
    _WORKER_QUEUE.put((fn, holder))
    if not done.wait(timeout):
        holder.setdefault("error", TimeoutError("браузер занят или завис (таймаут запроса)"))
    if holder.get("error"):
        raise holder["error"]
    return holder["result"]


def submit_without_wait(fn):
    _WORKER_QUEUE.put((fn, {"done": threading.Event()}))


def _worker_main():
    while True:
        fn, holder = _WORKER_QUEUE.get()
        try:
            holder["result"] = fn()
        except Exception as e:
            holder["error"] = e
        finally:
            holder["done"].set()


class BrowserManager:
    def __init__(self):
        self._pw = None
        self.browser = None
        self.context = None
        self.page = None
        self.profile = None
        self.refs = {}          # ref -> Playwright locator
        self.last_url = None    # последний посещённый URL (для open() без аргумента)
        self.last_used = 0.0

    # ---------- запуск / останов ----------

    def _proxy_from_env(self):
        raw = os.environ.get("https_proxy") or os.environ.get("http_proxy") \
            or os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")
        if not raw:
            return None
        m = re.match(r"^(\w+)://(?:([^:]+):([^@]+)@)?([^/]+?)(?::(\d+))?(?:/.*)?$", raw)
        if not m:
            return None
        scheme, user, pw, host, port = m.groups()
        server = f"{scheme}://{host}" + (f":{port}" if port else "")
        proxy = {"server": server}
        if user:
            proxy["username"] = user
            proxy["password"] = pw
        return proxy

    def ensure_started(self, profile="main"):
        if self.page is not None and not self.page.is_closed():
            return
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        self.profile = profile
        profile_path = PROFILE_DIR / profile
        profile_path.mkdir(parents=True, exist_ok=True)
        kwargs = {
            "user_data_dir": str(profile_path),
            "headless": False,
            "args": [
                "--disable-dev-shm-usage", "--no-sandbox", "--disable-gpu",
                "--proxy-bypass-list=<-loopback>;127.0.0.1;localhost;::1",
            ],
        }
        proxy = self._proxy_from_env()
        if proxy:
            kwargs["proxy"] = proxy
        # persistent-context: возвращает BrowserContext, вся папка профиля
        # (куки+IndexedDB+localStorage+ServiceWorkers) постоянна между запусками.
        self.context = self._pw.chromium.launch_persistent_context(**kwargs)
        self.browser = self.context
        self._migrate_legacy_cookies(profile, profile_path)
        self.context.add_init_script(INIT_SCRIPT)
        self.context.on("page", self._on_new_page)
        # У persistent-context уже есть стартовая страница about:blank
        self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
        self.page.set_default_timeout(ACTION_TIMEOUT)
        self.refs = {}
        self.last_used = time.time()
        self._load_last_url(profile)

    def _url_file(self, profile):
        return PROFILE_DIR / f"{profile}.url"

    def _load_last_url(self, profile):
        """Прочитать последний посещённый URL профиля (из файла при старте демона)."""
        uf = self._url_file(profile)
        try:
            u = uf.read_text(encoding="utf-8").strip()
            if u.startswith(("http://", "https://")):
                self.last_url = u
        except Exception:
            self.last_url = None

    def _save_last_url(self):
        u = self.last_url
        if not u or self.profile is None:
            return
        try:
            self._url_file(self.profile).write_text(u, encoding="utf-8")
        except Exception:
            pass

    def _migrate_legacy_cookies(self, profile, profile_path):
        """Одноразовый перенос кук из старого storage_state (*.json) в новый
        постоянный профиль. Нужен только при первом запуске после перехода на
        persistent-context (папка профиля пуста). Дальше куки хранятся сами."""
        try:
            is_new = not any(profile_path.iterdir())
        except Exception:
            is_new = False
        if not is_new:
            return
        state_file = STATE_DIR / f"{profile}.json"
        if not state_file.exists():
            return
        try:
            data = json.loads(state_file.read_text(encoding="utf-8"))
        except Exception:
            return
        cookies = data.get("cookies", []) if isinstance(data, dict) else []
        if not cookies:
            return
        import urllib.parse
        valid = []
        for c in cookies:
            try:
                if not c.get("name") or not c.get("value"):
                    continue
                if c.get("expires", -1) != -1 and c.get("expires", 0) < time.time():
                    continue
                valid.append({
                    "name": c["name"],
                    "value": c["value"],
                    "domain": c.get("domain", ""),
                    "path": c.get("path", "/"),
                    "expires": c.get("expires", -1),
                    "httpOnly": bool(c.get("httpOnly", False)),
                    "secure": bool(c.get("secure", False)),
                    "sameSite": c.get("sameSite", "Lax"),
                })
            except Exception:
                continue
        if valid:
            self.context.add_cookies(valid)
            logger.info(f"🍪 [BROWSER] Миграция: перенесено {len(valid)} кук из {state_file.name} в постоянный профиль")
        # Пометить, что миграция проведена (запись в неиспользуемый json — чтобы
        # на последующих запусках не перебирать снова; профиль уже непустой).

    def _close_nolock(self):
        try:
            if self.context:
                self.context.close()
        except Exception:
            pass
        self.browser = None
        self.context = None
        self.page = None
        self.refs = {}
        if self._pw:
            try:
                self._pw.stop()
            except Exception:
                pass
        self._pw = None

    def close(self):
        self._close_nolock()
        return {"closed": True}

    def maybe_idle_close(self):
        if self.context and (time.time() - self.last_used) > IDLE_CLOSE_SECONDS:
            # Запомнить текущую вкладку, чтобы после idle-закрытия её можно было
            # восстановить (reload/open без url откроют последний посещённый URL).
            try:
                u = self.page.url if self.page else ""
                if u and u.startswith(("http://", "https://")) and u != "about:blank":
                    self.last_url = u
                    self._save_last_url()
            except Exception:
                pass
            self._close_nolock()

    def save_state(self):
        # Постоянный профиль сам сохраняет всё состояние в свою папку.
        # Ничего писать не нужно.
        pass

    def _touch(self):
        self.last_used = time.time()

    def _on_new_page(self, page):
        """Страховка: закрыть лишнюю страницу, кроме единственной рабочей."""
        if page is self.page:
            return
        try:
            if len(self.context.pages) > 1:
                page.close()
        except Exception:
            pass

    def _prune_extra_pages(self):
        """Закрыть все страницы, кроме self.page (на случай late popup)."""
        try:
            for p in list(self.context.pages):
                if p is not self.page:
                    p.close()
        except Exception:
            pass

    # ---------- снапшот ----------

    def _collect_refs(self, max_refs, max_dups=3, skip_names=None):
        refs = []
        seen = {}
        skip_names = skip_names or set()
        try:
            locators = self.page.locator(SELECTOR).all()
        except Exception:
            return refs
        i = 0
        for loc in locators:
            if i >= max_refs:
                break
            try:
                if not loc.is_visible():
                    continue
                tag = (loc.evaluate("el => el.tagName.toLowerCase()") or "el").strip()
                name = ""
                for key in ("aria-label", "placeholder", "title", "value", "alt"):
                    v = loc.get_attribute(key)
                    if v:
                        name = re.sub(r"\s+", " ", (v or "")).strip()
                        break
                if not name:
                    # Поле без placeholder/aria-label: ищем подпись через <label for>
                    # или вложенный <label>/родитель с классом label. Также сам <label>.
                    try:
                        lbl = loc.evaluate("""el => {
                            if (el.id) {
                                const l = document.querySelector("label[for='" + CSS.escape(el.id) + "']");
                                if (l && l.textContent) return l.textContent.trim();
                            }
                            const lb = el.closest("label");
                            if (lb && lb.textContent && lb !== el) return lb.textContent.trim();
                            const inner = el.querySelector("label");
                            if (inner && inner.textContent) return inner.textContent.trim();
                            return "";
                        }""") or ""
                    except Exception:
                        lbl = ""
                    name = re.sub(r"\s+", " ", lbl).strip()
                if not name:
                    try:
                        txt = loc.inner_text(timeout=2000) or ""
                    except Exception:
                        txt = ""
                    name = re.sub(r"\s+", " ", txt).strip()
                href = ""
                if tag == "a":
                    href = (loc.get_attribute("href") or "").strip()
                # Мусор-фильтры: безликие ссылки-скрипты, новостные атрибуции,
                # HTML-обрывки в тексте, дубли заголовков (новостные карточки)
                if not name and tag not in ("input", "textarea", "select", "label"):
                    if href.startswith("javascript:") or not href:
                        continue
                if name.startswith("©") or name.startswith("<") and name.endswith(">"):
                    continue
                if name in skip_names and tag not in ("input", "textarea", "select"):
                    continue
                name = name[:80]
                extra = ""
                if tag == "a" and href.startswith(("http:", "https:", "/")):
                    path = href.split("?", 1)[0][:60]
                    extra = f" → {path}"
                if tag in ("input", "textarea", "select") and not name:
                    t = (loc.get_attribute("type") or tag)
                    name = f"{t}-поле"
                desc = f'{tag} "{name}"{extra}'.replace('""', "").strip()
                seen[desc] = seen.get(desc, 0) + 1
                if seen[desc] > max_dups:
                    continue
                ref = f"e{i}"
                refs.append({"ref": ref, "desc": desc})
                self.refs[ref] = loc
                i += 1
            except Exception:
                continue
        return refs

    def _snapshot_refs(self, max_refs=30, skip_names=None):
        """Список кликабельных элементов. Если пусто — JS не догрузился,
        ждём секунду (страницы вроде Bing дорисовывают контент после load)."""
        self.refs = {}
        refs = self._collect_refs(max_refs, skip_names=skip_names)
        if not refs:
            try:
                self.page.wait_for_timeout(1000)
            except Exception:
                pass
            self.refs = {}
            refs = self._collect_refs(max_refs, skip_names=skip_names)
        return refs

    def _page_headings(self, limit=200):
        """Заголовки h1–h3 — «о чём страница», без простыни body."""
        try:
            hs = self.page.locator("h1, h2, h3").all()
        except Exception:
            return ""
        seen = []
        for h in hs:
            try:
                if not h.is_visible():
                    continue
                t = (h.inner_text(timeout=2000) or "").strip().replace("\n", " ")
                if t and t not in seen:
                    seen.append(t)
            except Exception:
                continue
        text = " | ".join(seen)
        if len(text) > limit:
            text = text[:limit] + "…"
        return text

    def _page_text(self, limit=20000):
        try:
            t = self.page.inner_text("body", timeout=5000) or ""
        except Exception:
            t = ""
        t = re.sub(r"[ \t]+", " ", t)
        t = re.sub(r"\n{3,}", "\n\n", t).strip()
        if len(t) > limit:
            t = t[:limit] + f"\n…(обрезано, всего {len(t)} симв)"
        return t

    # ---------- операции ----------

    def open(self, url="", profile="main"):
        if not url or not url.strip():
            url = self.last_url
        if not url or not url.startswith(("http://", "https://")):
            raise ValueError(
                f"URL должен быть http(s), получил: {str(url)[:60]!r}. "
                "Передай url или открой предыдущую страницу, если она была."
            )
        if profile != (self.profile or "main"):
            self._close_nolock()
        self.ensure_started(profile)
        self._prune_extra_pages()
        self.page.goto(url, wait_until="load", timeout=NAV_TIMEOUT)
        try:
            self.page.wait_for_timeout(1500)
        except Exception:
            pass
        self.last_url = url
        self._save_last_url()
        self.save_state()
        self._touch()
        return self.snapshot(include_text=False)

    def snapshot(self, include_text=False, text_limit=1200):
        """Паспорт страницы: заголовки (о чём) + кликабельные элементы (что можно сделать).
        Текст страницы — только при include_text=True (или через dump)."""
        self.ensure_started()
        self._prune_extra_pages()
        headings = self._page_headings()
        skip = set(h.strip() for h in headings.split("|") if h.strip())
        refs = self._snapshot_refs(skip_names=skip)
        out = {
            "url": self.page.url,
            "title": self.page.title() if self.page else "",
            "headings": headings,
            "actions": "\n".join(f"{r['ref']}: {r['desc']}" for r in refs) or "(нет действий)",
        }
        if include_text:
            out["text"] = self._page_text(limit=text_limit)
        self._touch()
        return out

    def click(self, ref):
        self.ensure_started()
        self._prune_extra_pages()
        loc = self.refs.get(ref)
        if loc is None:
            self._snapshot_refs()
            if ref not in self.refs:
                raise ValueError(
                    f"ref {ref!r} невалиден (страница могла обновиться). Вызови snapshot() заново."
                )
            loc = self.refs[ref]
        loc.click(timeout=ACTION_TIMEOUT)
        self.save_state()
        self._touch()
        return self.snapshot(include_text=False)

    def fill(self, ref, value):
        self.ensure_started()
        self._prune_extra_pages()
        loc = self.refs.get(ref)
        if loc is None:
            raise ValueError(f"ref {ref!r} невалиден. Вызови snapshot() заново.")
        try:
            loc.fill(value, timeout=ACTION_TIMEOUT)
        except Exception:
            loc.click(timeout=ACTION_TIMEOUT)
            self.page.keyboard.type(value, delay=10)
        self.save_state()
        self._touch()
        return self.snapshot(include_text=False)

    def submit(self):
        self.ensure_started()
        self._prune_extra_pages()
        self.page.keyboard.press("Enter")
        self.page.wait_for_timeout(1500)
        self.save_state()
        self._touch()
        return self.snapshot(include_text=False)

    def reload(self):
        self.ensure_started()
        self._prune_extra_pages()
        try:
            cur = self.page.url
        except Exception:
            cur = ""
        # Если вкладки нет (браузер был убит по idle-таймауту, страница пуста) —
        # восстановить последний посещённый URL, а не крутить пустую about:blank.
        if not cur or cur in ("", "about:blank") or not cur.startswith(("http://", "https://")):
            if self.last_url:
                self.page.goto(self.last_url, wait_until="load", timeout=NAV_TIMEOUT)
            else:
                raise ValueError("Нет текущей страницы и нет сохранённого последнего URL — открой что-то через open(url).")
        else:
            self.page.reload(timeout=NAV_TIMEOUT)
        try:
            self.page.wait_for_timeout(1500)
        except Exception:
            pass
        self.last_url = cur if (cur and cur not in ("", "about:blank") and cur.startswith(("http://", "https://"))) else self.last_url
        self._save_last_url()
        self.save_state()
        self._touch()
        return self.snapshot(include_text=False)

    def dump(self, limit=20000):
        self.ensure_started()
        self._prune_extra_pages()
        text = self._page_text(limit=limit)
        self._touch()
        return {"url": self.page.url, "text": text}

    def screenshot(self):
        self.ensure_started()
        self._prune_extra_pages()
        SHOTS_DIR.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y%m%d-%H%M%S")
        path = SHOTS_DIR / f"shot-{ts}.png"
        self.page.screenshot(path=str(path), full_page=True)
        self._touch()
        return {"path": str(path.relative_to(WORKSPACE))}

    def status(self):
        running = bool(self.browser and self.page and not self.page.is_closed())
        if running:
            self._prune_extra_pages()
        info = {
            "running": running,
            "profile": self.profile,
            "idle_close_seconds": IDLE_CLOSE_SECONDS,
        }
        if running:
            info["url"] = self.page.url
            info["title"] = self.page.title()
            try:
                info["pages"] = len(self.context.pages)
            except Exception:
                info["pages"] = 1
        return info

    def reset_state(self, profile="main"):
        if profile == (self.profile or "main"):
            self._close_nolock()
            self.last_url = None
        f = PROFILE_DIR / profile
        if f.exists():
            import shutil
            shutil.rmtree(f, ignore_errors=True)
        uf = self._url_file(profile)
        if uf.exists():
            try:
                uf.unlink()
            except Exception:
                pass
        return {"reset": profile}


MGMT = BrowserManager()


def _idle_loop():
    while True:
        time.sleep(IDLE_CHECK_INTERVAL)
        submit_without_wait(MGMT.maybe_idle_close)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _respond(self, obj, code=200):
        data = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        try:
            self.wfile.write(data)
        except Exception:
            pass

    def do_GET(self):
        self._respond({"ok": True, "result": "browser daemon alive"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            req = json.loads(raw.decode("utf-8") or "{}")
        except ValueError:
            self._respond({"ok": False, "error": "bad json"})
            return
        action = req.pop("action", None)
        if not action:
            self._respond({"ok": False, "error": "missing 'action'"})
            return
        if not hasattr(MGMT, action):
            self._respond({"ok": False, "error": f"unknown action {action!r}"})
            return

        def job():
            return getattr(MGMT, action)(**req)

        try:
            result = run_in_browser_thread(job)
            self._respond({"ok": True, "result": result})
        except Exception as e:
            self._respond({"ok": False, "error": f"{type(e).__name__}: {e}"})


def main():
    for d in (STATE_DIR, SHOTS_DIR, PROFILE_DIR):
        d.mkdir(parents=True, exist_ok=True)
    threading.Thread(target=_worker_main, daemon=True).start()
    threading.Thread(target=_idle_loop, daemon=True).start()
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"browser daemon on http://{HOST}:{PORT}")
    srv.serve_forever()


if __name__ == "__main__":
    main()
