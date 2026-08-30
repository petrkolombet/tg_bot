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
import os
import queue
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

WORKSPACE = Path("/root/tg_bot/workspace")
STATE_DIR = WORKSPACE / "browser_state"
SHOTS_DIR = WORKSPACE / "browser_shots"
HOST, PORT = "127.0.0.1", 18933

NAV_TIMEOUT = int(os.environ.get("BROWSER_NAV_TIMEOUT_MS", "50000"))
ACTION_TIMEOUT = int(os.environ.get("BROWSER_ACTION_TIMEOUT_MS", "15000"))
IDLE_CLOSE_SECONDS = int(os.environ.get("BROWSER_IDLE_SECONDS", "300"))
IDLE_CHECK_INTERVAL = 10
REQUEST_TIMEOUT = int(os.environ.get("BROWSER_REQUEST_TIMEOUT", "65"))

SELECTOR = (
    "button, a[href], input, select, textarea, "
    "[role='button'], [role='link'], [role='textbox'], [role='checkbox'], "
    "[role='radio'], [role='combobox'], [role='option'], [role='tab']"
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
        state_file = STATE_DIR / f"{profile}.json"
        kwargs = {
            "headless": True,
            "args": [
                "--disable-dev-shm-usage", "--no-sandbox", "--disable-gpu",
                "--proxy-bypass-list=<-loopback>;127.0.0.1;localhost;::1",
            ],
        }
        proxy = self._proxy_from_env()
        if proxy:
            kwargs["proxy"] = proxy
        self.browser = self._pw.chromium.launch(**kwargs)
        if state_file.exists():
            self.context = self.browser.new_context(storage_state=str(state_file))
        else:
            self.context = self.browser.new_context()
        self.context.add_init_script(INIT_SCRIPT)
        self.context.on("page", self._on_new_page)
        self.page = self.context.new_page()
        self.page.set_default_timeout(ACTION_TIMEOUT)
        self.refs = {}
        self.last_used = time.time()

    def _close_nolock(self):
        try:
            if self.browser:
                self.browser.close()
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
        if self.browser and (time.time() - self.last_used) > IDLE_CLOSE_SECONDS:
            self._close_nolock()

    def save_state(self):
        try:
            f = STATE_DIR / f"{self.profile or 'main'}.json"
            f.parent.mkdir(parents=True, exist_ok=True)
            self.context.storage_state(path=str(f))
        except Exception:
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
                if not name and (href.startswith("javascript:") or not href):
                    continue
                if name.startswith("©") or name.startswith("<") and name.endswith(">"):
                    continue
                if name in skip_names:
                    continue
                name = name[:80]
                extra = ""
                if tag == "a" and href.startswith(("http:", "https:", "/")):
                    path = href.split("?", 1)[0][:60]
                    extra = f" → {path}"
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

    def open(self, url, profile="main"):
        if not url.startswith(("http://", "https://")):
            raise ValueError(f"URL должен быть http(s), получил: {url[:60]!r}")
        if profile != (self.profile or "main"):
            self._close_nolock()
        self.ensure_started(profile)
        self._prune_extra_pages()
        self.page.goto(url, wait_until="load", timeout=NAV_TIMEOUT)
        try:
            self.page.wait_for_timeout(1500)
        except Exception:
            pass
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
        self.page.reload(timeout=NAV_TIMEOUT)
        try:
            self.page.wait_for_timeout(1500)
        except Exception:
            pass
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
        f = STATE_DIR / f"{profile}.json"
        if f.exists():
            f.unlink()
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
    for d in (STATE_DIR, SHOTS_DIR):
        d.mkdir(parents=True, exist_ok=True)
    threading.Thread(target=_worker_main, daemon=True).start()
    threading.Thread(target=_idle_loop, daemon=True).start()
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"browser daemon on http://{HOST}:{PORT}")
    srv.serve_forever()


if __name__ == "__main__":
    main()