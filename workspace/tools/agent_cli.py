#!/usr/bin/env python3
"""Интерактивная работа с computer-агентом (Windows) через SSH-туннель.

Запуск:
  python3 agent_cli.py "текст задачи"
  python3 agent_cli.py status
  python3 agent_cli.py stop
  python3 agent_cli.py --watch   # периодически показывать статус/логи текущей задачи

URL берётся из computer_url.txt (http://127.0.0.1:8899).
"""
import json
import sys
import time
import urllib.request

BASE = "http://127.0.0.1:8899"


def _req(path, method="GET", data=None, timeout=15):
    url = BASE + path
    body = json.dumps(data).encode() if data is not None else None
    r = urllib.request.Request(url, data=body, method=method, headers={
        "Content-Type": "application/json",
    })
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(r, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode())
        except Exception:
            return {"status": "http_error", "detail": str(e)}


def show_status():
    d = _req("/status")
    print(f"status: {d.get('status')}")
    print(f"task:   {d.get('task')}")
    print(f"message: {d.get('message')}")
    for l in d.get("logs", []):
        print("  |", l)
    return d


def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return

    cmd = args[0]

    if cmd == "status":
        show_status()
    elif cmd == "stop":
        print(_req("/stop", "POST"))
    elif cmd == "--watch":
        last = 0
        while True:
            d = _req("/status")
            logs = d.get("logs", [])
            for l in logs[last:]:
                print("  |", l)
            last = len(logs)
            print(f"\n[{time.strftime('%H:%M:%S')}] status: {d.get('status')}")
            if d.get("status") in ("completed", "failed", "idle"):
                print("Done.")
                break
            time.sleep(5)
    else:
        task = " ".join(args)
        print(f"task: {task}")
        r = _req("/start", "POST", {"task": task})
        print(f"start => {r.get('status')} :: {r.get('message')}")
        if r.get("status") == "started":
            print("\nЛоги (обновление каждые 5 c, Ctrl+C для выхода):")
            last = 0
            try:
                while True:
                    d = _req("/status")
                    logs = d.get("logs", [])
                    for l in logs[last:]:
                        print("  |", l)
                    last = len(logs)
                    st = d.get("status")
                    if st in ("completed", "failed", "idle"):
                        print(f"\n=== {st.upper()} ===")
                        break
                    time.sleep(5)
            except KeyboardInterrupt:
                print("\n(остановлен вручную, задача может ещё идти — см. 'status')")


if __name__ == "__main__":
    main()