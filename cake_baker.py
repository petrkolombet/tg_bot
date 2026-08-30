#!/usr/bin/env python3
"""Демон PoW-бейкинга для g4f.space (gemini-фолбек).

Печёт "cakes" (proof-of-work) через прокси, чтобы копить кредиты на IP,
с которого tg-бот ходит к g4f.space. Кредиты тратятся на запросы Gemini.

Логика:
- каждый цикл: issue(UUIDs) -> SHA-256 нонс под difficulty -> bake -> +5¢;
- ночь/лимит: при достижении limit_per_day спит ~1 час (Retry-After из 429);
- работает бесконечно, Restart=on-failure в systemd.

Ничего не слушает (демон без порта).
"""

import hashlib
import json
import logging
import os
import random
import time
from urllib.request import Request, build_opener, HTTPBasicAuthHandler, ProxyHandler

import config

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("cake_baker")

CAKE_URL = "https://g4f.space/cake"
PROXY = config.G4F_PROXY  # idem config; задачи на одном IP
BATCH = 5                 # UUID за цикл (server caps at 50)
SLEEP_BETWEEN = 20        # сек между циклами (не долбим прокси)
SLEEP_AFTER_LIMIT = 3600  # сек сна при дневном лимите


def opener():
    handlers = []
    if PROXY:
        handlers.append(ProxyHandler({"http": PROXY, "https": PROXY}))
    else:
        op = build_opener()
        return op
    return build_opener(*handlers)


def req(method, url, body=None, timeout=30):
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/122",
        "Content-Type": "application/json" if body else "text/plain",
    }
    data = json.dumps(body).encode() if body else None
    r = Request(url, data=data, headers=headers, method=method)
    op = opener()
    resp = op.open(r, timeout=timeout)
    return json.loads(resp.read().decode())


def find_nonce(uuid, salt, difficulty, budget_s=90):
    target = 1 << (256 - difficulty)
    start = time.time()
    nonce = 0
    while time.time() - start < budget_s:
        nonce += 1
        digest = int.from_bytes(
            hashlib.sha256(f"{uuid}:{salt}:{nonce}".encode()).digest(), "big"
        )
        if digest < target:
            return nonce, format(digest, "064x")
    return None, None


def main():
    logger.info("cake_baker started (g4f PoW)")
    while True:
        try:
            status = req("GET", f"{CAKE_URL}/status")
            baked_today = status.get("baked_today", 0)
            limit_per_day = status.get("limit_per_day", 100)
            credit = status.get("credit_cents", 0)
            if baked_today >= limit_per_day:
                logger.info("daily limit reached (%s/%s), sleeping %ss",
                            baked_today, limit_per_day, SLEEP_AFTER_LIMIT)
                time.sleep(SLEEP_AFTER_LIMIT)
                continue

            issued = req("GET", f"{CAKE_URL}/issue?n={BATCH}")
            difficulty = issued.get("difficulty", 24)
            salt = issued.get("salt") or "g4f-default-salt"

            for uuid in issued.get("uuids", []):
                t0 = time.time()
                nonce, digest = find_nonce(uuid, salt, difficulty)
                if nonce is None:
                    logger.warning("nonce timeout for uuid=%s", uuid[:8])
                    continue
                try:
                    res = req("POST", f"{CAKE_URL}/bake", {
                        "uuid": uuid, "salt": salt, "nonce": nonce, "hash": digest,
                    })
                except Exception as e:
                    logger.error("bake error: %s", e)
                    time.sleep(5)
                    continue
                dt = time.time() - t0
                if res.get("ok"):
                    logger.info("baked %s nonce=%d %.0fs total=%sc credit=%s",
                                uuid[:8], nonce, dt,
                                res.get("total_credit_cents", credit),
                                res.get("credit", 0))
                else:
                    logger.warning("bake rejected: %s", res)
                time.sleep(1)

            time.sleep(SLEEP_BETWEEN + random.random() * 5)

        except Exception as e:
            logger.error("cycle error: %s", e)
            time.sleep(30)


if __name__ == "__main__":
    main()