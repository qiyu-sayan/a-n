import os
import sys
import time
import json
import yaml
import signal
import logging
import requests
import subprocess
from datetime import datetime, timezone

CONFIG_PATH = os.environ.get("BOT_CONFIG", "config.yaml")

def load_config(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    # 默认值
    cfg.setdefault("dry_run", True)
    cfg.setdefault("symbols", ["BTCUSDT"])
    cfg.setdefault("poll_seconds", 60)
    cfg.setdefault("log_level", "INFO")
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
    }
    # 用一行 JSON 当健康检查标记，方便日志检索
    logging.info(f"HEALTH {json.dumps(line, ensure_ascii=False)}")

class Stopper:
    def __init__(self):
        self.stop = False
        signal.signal(signal.SIGINT, self._sig)
        signal.signal(signal.SIGTERM, self._sig)
    def _sig(self, *_):
        self.stop = True

def fetch_price(symbol: str) -> float | None:
    url = "https://api.binance.com/api/v3/ticker/price"
    try:
        r = requests.get(url, params={"symbol": symbol}, timeout=10)
        if r.status_code != 200:
            logging.warning(f"{symbol} http {r.status_code}: {r.text[:200]}")
            return None
        data = r.json()
        return float(data["price"])
    except Exception as e:
        logging.warning(f"{symbol} fetch error: {e}")
        return None

def main():
    cfg = load_config(CONFIG_PATH)
    setup_logger(cfg["log_level"])
    health_line(cfg)

    stopper = Stopper()
    interval = max(5, int(cfg["poll_seconds"]))  # 最小 5s，避免太快
    dry_run = bool(cfg["dry_run"])
    symbols = list(cfg["symbols"])

    logging.info(f"Start loop: symbols={symbols}, interval={interval}s, dry_run={dry_run}")

    # ===== 你的策略逻辑入口（占位）=====
    # 你只需把计算/信号/下单封装成函数，在循环里调用即可。
    # 这里只做基础行情采集 + 打印示例。
    # =================================

    while not stopper.stop:
        start = time.time()
        for sym in symbols:
            px = fetch_price(sym)
            if px is None:
                continue

            # 打印一行“行情心跳”
            logging.info(f"PRICE {sym}={px:.2f}")

            # ==== 示例策略占位 ====
            # signal = your_strategy(px, sym)
            # if not dry_run and signal.should_trade:
            #     place_order(signal)
            #     logging.info(f"TRADE {signal}")
            # else:
            #     logging.debug(f"DRY {signal}")
            # ======================

        # 每轮输出一个健康行（便于长时间观测）
        health_line(cfg)

        # 睡眠至下个周期
        elapsed = time.time() - start
        time.sleep(max(0.0, interval - elapsed))

    logging.info("Graceful stop received. Bye.")

if __name__ == "__main__":
    main()