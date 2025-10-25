import os
import sys
import time
import json
import yaml
import signal
import logging
import requests
import subprocess
from statistics import mean
from datetime import datetime, timezone
from typing import Dict, Any

CONFIG_PATH = os.environ.get("BOT_CONFIG", "config.yaml")
STATE_PATH  = os.environ.get("BOT_STATE",  "state.json")

# ----------------- 基础配置/日志 -----------------
def load_config(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    cfg.setdefault("dry_run", True)
    cfg.setdefault("symbols", ["BTCUSDT"])
    cfg.setdefault("poll_seconds", 60)
    cfg.setdefault("log_level", "INFO")
    cfg.setdefault("strategy", {})
    return cfg

def setup_logger(level: str):
    lvl = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(
        level=lvl,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout)],
    )

def get_git_rev() -> str:
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            stderr=subprocess.DEVNULL,
        ).decode().strip()
        return out
    except Exception:
        return "unknown"

def health_line(cfg: dict):
    line = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "app": "crypto-bot",
        "version": get_git_rev(),
        "mode": "DRY_RUN" if cfg.get("dry_run") else "LIVE",
        "symbols": cfg.get("symbols"),
        "poll_seconds": cfg.get("poll_seconds"),
        "strategy": cfg.get("strategy"),
    }
    logging.info(f"HEALTH {json.dumps(line, ensure_ascii=False)}")

class Stopper:
    def __init__(self):
        self.stop = False
        signal.signal(signal.SIGINT, self._sig)
        signal.signal(signal.SIGTERM, self._sig)
    def _sig(self, *_):
        self.stop = True

# ----------------- 行情与工具 -----------------
BINANCE_BASE = "https://api.binance.com"

def fetch_price(symbol: str) -> float | None:
    try:
        r = requests.get(f"{BINANCE_BASE}/api/v3/ticker/price",
                         params={"symbol": symbol}, timeout=10)
        r.raise_for_status()
        return float(r.json()["price"])
    except Exception as e:
        logging.warning(f"{symbol} price error: {e}")
        return None

def fetch_klines(symbol: str, interval: str, limit: int = 60) -> list[float]:
    """返回收盘价列表（float）"""
    try:
        r = requests.get(f"{BINANCE_BASE}/api/v3/klines",
                         params={"symbol": symbol, "interval": interval, "limit": limit},
                         timeout=10)
        r.raise_for_status()
        data = r.json()
        closes = [float(x[4]) for x in data]  # index 4 是 close
        return closes
    except Exception as e:
        logging.warning(f"{symbol} klines error: {e}")
        return []

# ----------------- 状态持久化 -----------------
def load_state() -> Dict[str, Any]:
    if not os.path.exists(STATE_PATH):
        return {}
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def save_state(state: Dict[str, Any]):
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_PATH)

def pos_key(sym: str) -> str:
    return f"pos::{sym}"

# ----------------- 策略：双均线 + 止盈/止损 -----------------
def sma(values: list[float], n: int) -> float | None:
    if len(values) < n:
        return None
    return mean(values[-n:])

def strategy_sma(symbol: str, cfg: dict, state: dict):
    sconf = cfg["strategy"]
    fast = int(sconf.get("fast", 12))
    slow = int(sconf.get("slow", 26))
    interval = sconf.get("interval", "1m")
    qty = float(sconf.get("qty", 0.001))
    tp = float(sconf.get("take_profit", 0.03))
    sl = float(sconf.get("stop_loss", 0.01))

    closes = fetch_klines(symbol, interval, limit=max(slow + 3, 60))
    if len(closes) < slow + 1:
        logging.info(f"{symbol} wait more candles... ({len(closes)}/{slow+1})")
        return

    fast_now = sma(closes, fast)
    slow_now = sma(closes, slow)
    fast_prev = sma(closes[:-1], fast)
    slow_prev = sma(closes[:-1], slow)
    last = closes[-1]

    if fast_now is None or slow_now is None or fast_prev is None or slow_prev is None:
        return

    k = pos_key(symbol)
    pos = state.get(k)  # None or dict: {side, entry, qty}

    # 交叉信号
    cross_up   = fast_prev is not None and slow_prev is not None and fast_prev <= slow_prev and fast_now >  slow_now
    cross_down = fast_prev is not None and slow_prev is not None and fast_prev >= slow_prev and fast_now <  slow_now

    # 有仓位：检查止盈止损
    if pos:
        pnl = (last - pos["entry"]) / pos["entry"] * (1 if pos["side"] == "LONG" else -1)
        if pnl >= tp:
            logging.info(f"SIGNAL TAKE_PROFIT {symbol} side={pos['side']} entry={pos['entry']:.2f} last={last:.2f} pnl={pnl:.4f}")
            state.pop(k, None)
            save_state(state)
            return
        if pnl <= -sl:
            logging.info(f"SIGNAL STOP_LOSS  {symbol} side={pos['side']} entry={pos['entry']:.2f} last={last:.2f} pnl={pnl:.4f}")
            state.pop(k, None)
            save_state(state)
            return

    # 无仓位：看交叉入场
    if not pos:
        if cross_up:
            state[k] = {"side": "LONG", "entry": last, "qty": qty}
            save_state(state)
            logging.info(f"SIGNAL BUY  {symbol} entry={last:.2f} fast={fast_now:.2f} slow={slow_now:.2f}")
            return
        if cross_down:
            state[k] = {"side": "SHORT", "entry": last, "qty": qty}
            save_state(state)
            logging.info(f"SIGNAL SELL {symbol} entry={last:.2f} fast={fast_now:.2f} slow={slow_now:.2f}")
            return

    # 常规价格心跳（可选）
    logging.info(f"PRICE {symbol}={last:.2f} fast={fast_now:.2f} slow={slow_now:.2f} {'HOLD' if pos else 'FLAT'}")

# ----------------- 主循环 -----------------
def main():
    cfg = load_config(CONFIG_PATH)
    setup_logger(cfg["log_level"])
    health_line(cfg)

    stopper = Stopper()
    interval = max(30, int(cfg["poll_seconds"]))  # 最小 30s，避免过于频繁
    dry_run = bool(cfg["dry_run"])
    symbols = list(cfg["symbols"])
    strat = cfg.get("strategy", {})
    if strat.get("type", "sma").lower() != "sma":
        logging.error("仅内置了 strategy.type=sma 的样板")
        sys.exit(1)

    state = load_state()
    logging.info(f"Start loop: symbols={symbols}, interval={interval}s, dry_run={dry_run}, strategy=sma")

    while not stopper.stop:
        start = time.time()
        for sym in symbols:
            try:
                strategy_sma(sym, cfg, state)
            except Exception as e:
                logging.warning(f"{sym} strategy error: {e}")
        health_line(cfg)
        elapsed = time.time() - start
        time.sleep(max(0.0, interval - elapsed))

    logging.info("Graceful stop received. Bye.")

if __name__ == "__main__":
    main()