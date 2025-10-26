# bot/main.py
# -*- coding: utf-8 -*-
"""
Minimal Binance bot runner for GitHub Actions
- Pulls klines via REST
- Simple SMA strategy (fast vs slow)
- Testnet/Mainnet switch via env
- Time-bounded run via RUN_MINUTES
- WeCom webhook notification (optional)

Env vars (set in GitHub Actions 'env:' or repo Secrets):
  BINANCE_BASE_URL   (default: https://api.binance.com)       # Testnet -> https://testnet.binance.vision/api
  BINANCE_TESTNET    ("1" to enable testnet)
  BINANCE_KEY        (optional, only needed if you place orders; this demo just logs signals)
  BINANCE_SECRET     (optional)
  SYMBOLS            (comma separated, default: "BTCUSDT,ETHUSDT")
  INTERVAL           (default: "1m")
  FAST               (fast SMA len, default: 12)
  SLOW               (slow SMA len, default: 26)
  RUN_MINUTES        (default: 2)
  WECOM_WEBHOOK      (optional; enterprise wechat robot webhook)
"""

import os
import time
import hmac
import hashlib
import logging
import json
from datetime import datetime, timezone
from typing import List, Tuple

import requests

# ---------- ENV & Defaults ----------
BINANCE_TESTNET = os.getenv("BINANCE_TESTNET", "0") == "1"
BINANCE_BASE_URL = os.getenv(
    "BINANCE_BASE_URL",
    "https://testnet.binance.vision/api" if BINANCE_TESTNET else "https://api.binance.com"
)
BINANCE_KEY = os.getenv("BINANCE_KEY", "")
BINANCE_SECRET = os.getenv("BINANCE_SECRET", "")

SYMBOLS = [s.strip().upper() for s in os.getenv("SYMBOLS", "BTCUSDT,ETHUSDT").split(",") if s.strip()]
INTERVAL = os.getenv("INTERVAL", "1m")
FAST = int(os.getenv("FAST", "12"))
SLOW = int(os.getenv("SLOW", "26"))

RUN_MINUTES = int(os.getenv("RUN_MINUTES", "2"))
RUN_END_TS = time.time() + RUN_MINUTES * 60

WECOM_WEBHOOK = os.getenv("WECOM_WEBHOOK", "").strip()

# ---------- Logging ----------
os.makedirs("logs", exist_ok=True)
log_path = os.path.join("logs", f"{datetime.now(timezone.utc).date()}.log")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(log_path, encoding="utf-8")
    ]
)
log = logging.getLogger("bot")

def notify_wecom(text: str) -> None:
    """Send plain text to WeCom robot if webhook provided."""
    if not WECOM_WEBHOOK:
        return
    try:
        payload = {"msgtype": "text", "text": {"content": text[:4000]}}
        r = requests.post(WECOM_WEBHOOK, json=payload, timeout=10)
        if r.status_code != 200:
            log.warning("WeCom push non-200: %s %s", r.status_code, r.text[:200])
    except Exception as e:
        log.warning("WeCom push error: %s", e)

# ---------- Binance REST tiny helpers ----------
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "a-n/gh-actions-bot"})

def _get(path: str, params: dict = None, timeout: int = 15):
    url = BINANCE_BASE_URL.rstrip("/") + path
    r = SESSION.get(url, params=params or {}, timeout=timeout)
    # GitHub Actions 451/403 常见，这里明确打日志
    if r.status_code != 200:
        log.warning("HTTP %s for %s params=%s body=%s", r.status_code, path, params, r.text[:200])
        r.raise_for_status()
    return r.json()

def get_klines(symbol: str, interval: str, limit: int = 200) -> List[Tuple[float, float]]:
    """
    Return list of (close_time, close_price) for given symbol/interval.
    """
    data = _get("/api/v3/klines", {"symbol": symbol, "interval": interval, "limit": limit})
    closes = [(int(k[6]) / 1000.0, float(k[4])) for k in data]  # close time (s), close price
    return closes

# ---------- Strategy ----------
def sma(values: List[float], n: int) -> List[float]:
    if n <= 0:
        raise ValueError("SMA window must be > 0")
    out = []
    s = 0.0
    for i, v in enumerate(values):
        s += v
        if i >= n:
            s -= values[i - n]
        if i >= n - 1:
            out.append(s / n)
        else:
            out.append(float("nan"))
    return out

def generate_signal(prices: List[float], fast: int, slow: int) -> str:
    """
    Returns "BUY", "SELL", or "HOLD" based on fast/slow SMA cross.
    """
    if slow <= fast:
        slow, fast = fast, slow  # ensure slow >= fast
    if len(prices) < slow + 2:
        return "HOLD"

    f = sma(prices, fast)
    s = sma(prices, slow)

    # look at last two bars for cross
    f1, s1 = f[-1], s[-1]
    f2, s2 = f[-2], s[-2]

    if f2 <= s2 and f1 > s1:
        return "BUY"
    if f2 >= s2 and f1 < s1:
        return "SELL"
    return "HOLD"

# ---------- (Optional) place order on testnet ----------
def _sign(params: dict, secret: str) -> str:
    query = "&".join([f"{k}={params[k]}" for k in sorted(params.keys())])
    return hmac.new(secret.encode(), query.encode(), hashlib.sha256).hexdigest()

def place_testnet_order(symbol: str, side: str, qty: float) -> dict:
    """
    Very small example for MARKET order on /api/v3/order (SIGNED).
    Only if you configured BINANCE_KEY/SECRET and you're on testnet (recommended).
    """
    if not BINANCE_KEY or not BINANCE_SECRET:
        raise RuntimeError("No API key/secret; skip placing order.")

    ts = int(time.time() * 1000)
    params = {
        "symbol": symbol,
        "side": side.upper(),
        "type": "MARKET",
        "quantity": qty,          # NOTE: must fit testnet lot size; this is demo only
        "timestamp": ts
    }
    params["signature"] = _sign(params, BINANCE_SECRET)
    url = BINANCE_BASE_URL.rstrip("/") + "/api/v3/order"
    headers = {"X-MBX-APIKEY": BINANCE_KEY}
    r = SESSION.post(url, headers=headers, params=params, timeout=15)
    if r.status_code != 200:
        log.warning("Order HTTP %s: %s", r.status_code, r.text[:200])
        r.raise_for_status()
    return r.json()

# ---------- Main loop (time-bounded) ----------
def main():
    start_msg = f"START {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}\n" \
                f"symbols={SYMBOLS} interval={INTERVAL} fast={FAST} slow={SLOW}\n" \
                f"base_url={BINANCE_BASE_URL} testnet={BINANCE_TESTNET}"
    log.info(start_msg)
    notify_wecom(f"[a-n] Bot start\n{start_msg}")

    while time.time() < RUN_END_TS:
        for sym in SYMBOLS:
            try:
                kl = get_klines(sym, INTERVAL, limit=max(200, SLOW + 5))
                closes = [p for _, p in kl]
                signal = generate_signal(closes, FAST, SLOW)
                last_price = closes[-1] if closes else float("nan")
                log.info("symbol=%s price=%.6f signal=%s", sym, last_price, signal)

                # Demo: 仅在 Testnet、且提供了 key/secret 时，尝试下单（非常小的 quantity；可能因精度失败）
                # 实盘请自己处理风控与数量精度，这里只是示例。
                # if BINANCE_TESTNET and BINANCE_KEY and BINANCE_SECRET and signal in ("BUY", "SELL"):
                #     side = "BUY" if signal == "BUY" else "SELL"
                #     resp = place_testnet_order(sym, side, qty=0.001)
                #     log.info("order resp: %s", json.dumps(resp)[:200])

            except requests.HTTPError as he:
                # 451/403/429 等在此都会被记录
                log.warning("HTTP error for %s: %s", sym, he)
            except Exception as e:
                log.warning("Error for %s: %s", sym, e)

        # 小睡一会，避免频繁请求
        time.sleep(5)

    done_msg = f"DONE {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}"
    log.info(done_msg)
    notify_wecom(f"[a-n] Bot finished\n{done_msg}")

if __name__ == "__main__":
    main()