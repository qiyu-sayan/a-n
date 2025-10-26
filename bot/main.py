# bot/main.py
# -*- coding: utf-8 -*-

import os
import time
import json
import logging
import requests
from dotenv import load_dotenv
from binance.spot import Spot as Client

# ========= 环境 & 日志 =========
load_dotenv()  # 允许（可选）从 .env 读取本地变量；Actions 用 env 注入即可
os.makedirs("logs", exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("logs/latest.log", encoding="utf-8"),
        logging.StreamHandler()
    ]
)
log = logging.getLogger("bot")

# ========= 配置项（来自环境变量，可在 Actions 里改）=========
ENABLE_TRADING = os.getenv("ENABLE_TRADING", "0") == "1"   # 1=允许交易；默认 0(禁止)
PAPER          = os.getenv("PAPER", "1") == "1"            # 1=纸交易/测试单；0=真单
ORDER_USDT     = float(os.getenv("ORDER_USDT", "10"))
SYMBOLS        = [s.strip().upper() for s in os.getenv("SYMBOLS", "BTCUSDT,ETHUSDT").split(",") if s.strip()]
INTERVAL       = os.getenv("INTERVAL", "1m")
FAST           = int(os.getenv("FAST", "12"))
SLOW           = int(os.getenv("SLOW", "26"))
RUN_MINUTES    = int(os.getenv("RUN_MINUTES", "2"))

# ========= WeCom（企业微信）推送 =========
def _get_wechat_hook() -> str:
    # 同时兼容 WECHAT_WEBHOOK / WECOM_WEBHOOK，哪个有用哪个
    return os.getenv("WECHAT_WEBHOOK") or os.getenv("WECOM_WEBHOOK") or ""

def wechat(text: str):
    hook = _get_wechat_hook()
    if not hook:
        log.warning("WeCom webhook is EMPTY; skip push.")
        return
    try:
        r = requests.post(
            hook,
            json={"msgtype": "text", "text": {"content": text[:4000]}},
            timeout=8
        )
        if r.status_code != 200 or '"errcode":0' not in r.text:
            log.warning(f"WeCom push non-OK: HTTP {r.status_code} body={r.text[:200]}")
    except Exception as ex:
        log.warning(f"WeCom push failed: {ex}")

# ========= Binance 客户端 =========
def make_client() -> Client:
    api_key = os.getenv("BINANCE_KEY", "")
    api_secret = os.getenv("BINANCE_SECRET", "")
    testnet = os.getenv("BINANCE_TESTNET", "0") == "1"
    if testnet:
        base_url = "https://testnet.binance.vision"
        log.info("Using Binance Testnet ✅")
    else:
        base_url = None
        log.info("Using Binance Mainnet 🌍")
    return Client(key=api_key, secret=api_secret, base_url=base_url)

client = make_client()

# ========= 工具 & 容错 =========
_warned_451 = set()

def is_region_restricted(e: Exception) -> bool:
    msg = str(e).lower()
    return ("451" in msg) or ("restricted location" in msg) or ("eligibility" in msg)

def fetch_klines_safe(symbol: str, interval: str = "1m", limit: int = 200, max_retries: int = 2):
    """带轻量重试 + 451 软跳过的 K 线请求"""
    backoff = 1.5
    for i in range(max_retries + 1):
        try:
            return client.klines(symbol, interval=interval, limit=limit)
        except Exception as e:
            if is_region_restricted(e):
                if symbol not in _warned_451:
                    _warned_451.add(symbol)
                    msg = f"[{symbol}] 451/地区限制，已跳过该标的。"
                    log.warning(msg)
                    wechat(f"[跳过] {msg}")
                return None
            if i < max_retries:
                sleep_s = backoff ** i
                log.warning(f"[{symbol}] 拉K线异常：{e}，{sleep_s:.1f}s后重试（{i+1}/{max_retries}）")
                time.sleep(sleep_s)
            else:
                raise

def last_price(symbol: str):
    k = fetch_klines_safe(symbol, INTERVAL, 2)
    if not k:
        return None
    return float(k[-1][4])

# ========= 简单信号（示例：价格 vs SMA）=========
def simple_signal(symbol: str):
    k = fetch_klines_safe(symbol, INTERVAL, max(50, SLOW + 5))
    if not k:
        return None
    closes = [float(x[4]) for x in k]
    price  = closes[-1]
    sma_fast = sum(closes[-FAST:]) / FAST
    sma_slow = sum(closes[-SLOW:]) / SLOW
    log.info(f"[{symbol}] price={price:.4f} fast={sma_fast:.4f} slow={sma_slow:.4f}")

    if sma_fast > sma_slow * 1.001:   # 稍微加一点阈值，避免抖动
        return {"side": "BUY", "price": price}
    if sma_fast < sma_slow * 0.999:
        return {"side": "SELL", "price": price}
    return None

# ========= 下单（纸交易/测试单/真单，带护栏）=========
def place_order(symbol: str, side: str, usdt_notional: float):
    """
    - PAPER=True 或 ENABLE_TRADING=False 时：只走 new_order_test（测试单，不会产生真实委托）
    - 真仓只有在 (ENABLE_TRADING=True 且 PAPER=False 且 非测试网) 时才会执行
    """
    price = last_price(symbol)
    if price is None or price <= 0:
        log.warning(f"[{symbol}] 无法获取价格，跳过下单")
        return {"status": "skip", "reason": "no_price"}

    # 简单按名义 USDT 计算数量；实际交易应读取交易规则对齐精度与最小数量
    qty = max(0.000001, round(usdt_notional / price, 6))
    log.info(f"[{symbol}] 准备下单 side={side} qty≈{qty} (~{usdt_notional} USDT)")

    # 纸交易 or 未开启交易 -> 测试单
    if PAPER or not ENABLE_TRADING:
        try:
            client.new_order_test(symbol, side, "MARKET", quantity=qty)
            log.info(f"[PAPER] {symbol} {side} 测试单已提交")
            wechat(f"[PAPER] {symbol} {side} qty≈{qty}")
            return {"status": "paper_ok", "qty": qty}
        except Exception as e:
            log.warning(f"[PAPER.FAIL] {symbol} {side} -> {e}")
            wechat(f"[PAPER.FAIL] {symbol} {side} -> {e}")
            return {"status": "paper_fail", "error": str(e)}

    # 真仓
    try:
        res = client.new_order(symbol, side, "MARKET", quantity=qty)
        brief = json.dumps(res)[:300]
        log.info(f"[LIVE] 下单成功：{brief}...")
        wechat(f"[LIVE] {symbol} {side} qty≈{qty}")
        return {"status": "live_ok", "qty": qty}
    except Exception as e:
        log.error(f"[LIVE.FAIL] {symbol} {side} -> {e}")
        wechat(f"[LIVE.FAIL] {symbol} {side} -> {e}")
        return {"status": "live_fail", "error": str(e)}

# ========= 主流程 =========
def main():
    start_ts = time.time()
    end_ts = start_ts + RUN_MINUTES * 60
    wechat("▶️ Bot run start")
    log.info(f"🚀 run start | SYMBOLS={SYMBOLS} ENABLE_TRADING={ENABLE_TRADING} PAPER={PAPER} ORDER_USDT={ORDER_USDT}")

    acted = 0
    while time.time() < end_ts:
        for sym in SYMBOLS:
            sig = simple_signal(sym)
            if not sig:
                continue
            res = place_order(sym, sig["side"], ORDER_USDT)
            acted += 1
            time.sleep(0.2)  # 轻微节流
        # 每轮间隔（不要打太快，避免限频）
        time.sleep(5)

    if acted == 0:
        log.info("本次无交易动作（可能无信号或全部被 451 跳过）。")
    wechat("✅ Bot run end")
    log.info("🏁 run end")

if __name__ == "__main__":
    main()