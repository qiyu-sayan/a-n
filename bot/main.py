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

from trader import Trader, TraderConfig

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

# ----------------- 行情 -----------------
BINANCE_BASE = "https://api.binance.com"

def fetch_klines(symbol: str, interval: str, limit: int = 60) -> list[float]:
    try:
        r = requests.get(f"{BINANCE_BASE}/api/v3/klines",
                         params={"symbol": symbol, "interval": interval, "limit": limit},
                         timeout=10)
        r.raise_for_status()
        data = r.json()
        closes = [float(x[4]) for x in data]
        return closes
    except Exception as e:
        logging.warning(f"{symbol} klines error: {e}")
        return [],[]

# ----------------- 状态 -----------------
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

# ----------------- 策略核心：双均线 + 止盈止损 -----------------
def sma(values: list[float], n: int) -> float | None:
    if len(values) < n:
        return None
    return mean(values[-n:])

def strategy_sma(symbol: str, cfg: dict, state: dict, trader: Trader):
    sconf = cfg["strategy"]
    fast = int(sconf.get("fast", 12))
    slow = int(sconf.get("slow", 26))
    interval = sconf.get("interval", "1m")
    qty = float(sconf.get("qty", 0.001))
    tp_ratio = float(sconf.get("take_profit", 0.03))
    sl_ratio = float(sconf.get("stop_loss", 0.01))

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
    pos = state.get(k)  # None or {side, entry, qty}

    cross_up   = fast_prev <= slow_prev and fast_now >  slow_now
    cross_down = fast_prev >= slow_prev and fast_now <  slow_now

    # 有仓位：浮动止盈/止损（仅对 LONG 示范）
    if pos and pos["side"] == "LONG":
        pnl = (last - pos["entry"]) / pos["entry"]
        if pnl >= tp_ratio:
            logging.info(f"SIGNAL TAKE_PROFIT {symbol} entry={pos['entry']:.2f} last={last:.2f} pnl={pnl:.4f}")
            # 市价卖出（真实模式），dry_run 会自动只打印
            trader.market_order(symbol, "SELL", pos["qty"])
            state.pop(k, None)
            save_state(state)
            return
        if pnl <= -sl_ratio:
            logging.info(f"SIGNAL STOP_LOSS  {symbol} entry={pos['entry']:.2f} last={last:.2f} pnl={pnl:.4f}")
            trader.market_order(symbol, "SELL", pos["qty"])
            state.pop(k, None)
            save_state(state)
            return

    # 无仓：看金叉做多（示范 LONG；做空现货需用合约/杠杆，本文不展开）
    if not pos and cross_up:
        state[k] = {"side": "LONG", "entry": last, "qty": qty}
        save_state(state)
        logging.info(f"SIGNAL BUY {symbol} entry={last:.2f} fast={fast_now:.2f} slow={slow_now:.2f}")
        trader.market_order(symbol, "BUY", qty)
        # 如需在现货上用 OCO（一次挂出止盈+止损），示例：
        # tp = round(last * (1 + tp_ratio), 2)
        # sp = round(last * (1 - sl_ratio), 2)
        # sl = round(last * (1 - sl_ratio * 1.002), 2)  # stopLimit 比 stopPrice 略低一点
        # trader.oco(symbol, qty, take_profit_price=tp, stop_price=sp, stop_limit_price=sl)
        return

    logging.info(f"PRICE {symbol}={last:.2f} fast={fast_now:.2f} slow={slow_now:.2f} {'HOLD' if pos else 'FLAT'}")

# ----------------- 主循环 -----------------
def main():
    cfg = load_config(CONFIG_PATH)
    setup_logger(cfg["log_level"])
    health_line(cfg)

    trader = Trader(TraderConfig(
        dry_run=bool(cfg.get("dry_run", True)),
        testnet=(os.getenv("BINANCE_TESTNET", "1") == "1")
    ))

    stopper = Stopper()
    interval = max(30, int(cfg["poll_seconds"]))
    symbols = list(cfg["symbols"])

    if cfg.get("strategy", {}).get("type", "sma").lower() != "sma":
        logging.error("仅内置 strategy.type=sma")
        sys.exit(1)

    state = load_state()
    logging.info(f"Start loop: symbols={symbols}, interval={interval}s, dry_run={cfg.get('dry_run', True)}, strategy=sma")

    while not stopper.stop:
        start = time.time()
        for sym in symbols:
            try:
                strategy_sma(sym, cfg, state, trader)
            except Exception as e:
                logging.warning(f"{sym} strategy error: {e}")
        health_line(cfg)
        elapsed = time.time() - start
        time.sleep(max(0.0, interval - elapsed))

    logging.info("Graceful stop received. Bye.")

if __name__ == "__main__":
    main()