import os, time, json, csv, math, sys, signal
import requests
import pandas as pd
import numpy as np
import yaml
from datetime import datetime, timezone

# ---------- utils ----------
def utcnow_str():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S%z")

def ensure_dir(p):
    os.makedirs(p, exist_ok=True)

def load_config(path):
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)

def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = (delta.clip(lower=0)).ewm(alpha=1/period, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1/period, adjust=False).mean()
    rs = gain / (loss.replace(0, np.nan))
    return 100 - (100 / (1 + rs))

def fetch_klines(symbol: str, interval: str, limit: int = 200) -> pd.DataFrame:
    # Binance 公共 K 线（不需要密钥）
    url = "https://api.binance.com/api/v3/klines"
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    r = requests.get(url, params=params, timeout=10)
    r.raise_for_status()
    raw = r.json()
    cols = ["open_time","open","high","low","close","volume","close_time",
            "qav","trades","taker_base","taker_quote","ignore"]
    df = pd.DataFrame(raw, columns=cols)
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    df["close_time"] = pd.to_datetime(df["close_time"], unit="ms", utc=True)
    for c in ["open","high","low","close","volume","qav","taker_base","taker_quote"]:
        df[c] = df[c].astype(float)
    return df

# ---------- paper portfolio ----------
class PaperBroker:
    def __init__(self, log_dir, fee):
        self.state_file = os.path.join(log_dir, "paper_state.json")
        self.trades_file = os.path.join(log_dir, "trades.csv")
        self.fee = fee
        ensure_dir(log_dir)
        if not os.path.exists(self.trades_file):
            with open(self.trades_file, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(["ts_utc","side","price","qty","notional","fee","position_after","cash_after"])
        if os.path.exists(self.state_file):
            with open(self.state_file, "r", encoding="utf-8") as f:
                self.state = json.load(f)
        else:
            # 初始 1000 USDT 纸面资产（可改）
            self.state = {"position": 0.0, "cash": 1000.0}
            self._save()

    def _save(self):
        with open(self.state_file, "w", encoding="utf-8") as f:
            json.dump(self.state, f)

    def buy(self, price, notional):
        qty = notional / price
        fee = notional * self.fee
        if self.state["cash"] < notional + fee:
            return False, "INSUFFICIENT_CASH"
        self.state["cash"] -= (notional + fee)
        self.state["position"] += qty
        self._save()
        self._log_trade("BUY", price, qty, notional, fee)
        return True, "OK"

    def sell(self, price, qty):
        if self.state["position"] < qty:
            return False, "INSUFFICIENT_POSITION"
        notional = qty * price
        fee = notional * self.fee
        self.state["cash"] += (notional - fee)
        self.state["position"] -= qty
        self._save()
        self._log_trade("SELL", price, qty, notional, fee)
        return True, "OK"

    def _log_trade(self, side, price, qty, notional, fee):
        with open(self.trades_file, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow([utcnow_str(), side, price, qty, notional, fee, self.state["position"], self.state["cash"]])

# ---------- main bot ----------
def main():
    cfg_path = os.environ.get("CRYPTO_BOT_CONFIG", "/opt/projects/a-n/config.yaml")
    cfg = load_config(cfg_path)

    log_dir = cfg.get("log_dir", "/var/log/crypto-bot")
    ensure_dir(log_dir)
    app_log = os.path.join(log_dir, "app.log")

    def log(msg):
        line = f"[{utcnow_str()}] {msg}"
        print(line, flush=True)
        with open(app_log, "a", encoding="utf-8") as f:
            f.write(line + "\n")

    # 纸面经纪
    broker = PaperBroker(log_dir, fee=cfg["taker_fee"])

    loop_sec = int(cfg.get("loop_interval_sec", 5))
    symbol = cfg["symbol"]
    tf = cfg["timeframe"]
    fast = int(cfg["sma_fast"])
    slow = int(cfg["sma_slow"])
    rsi_p = int(cfg["rsi_period"])
    rsi_buy = float(cfg["rsi_buy"])
    rsi_sell = float(cfg["rsi_sell"])
    trade_notional = float(cfg["trade_notional_usdt"])

    # 优雅退出
    stop = False
    def handler(sig, frame): 
        nonlocal stop
        stop = True
        log("stop signal received")
    signal.signal(signal.SIGINT, handler)
    signal.signal(signal.SIGTERM, handler)

    log(f"Bot start: mode={cfg['mode']} symbol={symbol} tf={tf} loop={loop_sec}s")

    last_cross = None  # 记录上一次均线方向：'golden' 或 'dead'
    while not stop:
        try:
            df = fetch_klines(symbol, tf, limit=max(200, slow + 50))
            close = df["close"]
            df["sma_fast"] = close.rolling(fast).mean()
            df["sma_slow"] = close.rolling(slow).mean()
            df["rsi"] = rsi(close, rsi_p)

            row = df.iloc[-1]
            price = float(row["close"])
            fma = float(row["sma_fast"])
            sma = float(row["sma_slow"])
            rsi_now = float(row["rsi"])

            cross = None
            prev = df.iloc[-2]
            if prev["sma_fast"] <= prev["sma_slow"] and fma > sma:
                cross = "golden"
            elif prev["sma_fast"] >= prev["sma_slow"] and fma < sma:
                cross = "dead"

            pos = broker.state["position"]
            cash = broker.state["cash"]

            # 生成信号（很简单的示例策略）
            executed = ""
            if cross == "golden" and rsi_now >= rsi_buy and cash > trade_notional:
                ok, msg = broker.buy(price, trade_notional)
                executed = f"BUY {trade_notional}USDT @ {price} -> {msg}"
                last_cross = "golden"
            elif cross == "dead" and rsi_now <= rsi_sell and pos > 0:
                # 卖掉一半仓位作为示例
                qty = max(pos * 0.5, 0.0001)
                ok, msg = broker.sell(price, qty)
                executed = f"SELL {qty:.6f} @ {price} -> {msg}"
                last_cross = "dead"

            log(f"price={price:.2f} fma={fma:.2f} sma={sma:.2f} rsi={rsi_now:.1f} "
                f"pos={pos:.6f} cash={cash:.2f} cross={cross} {executed}")

        except Exception as e:
            log(f"ERROR: {repr(e)}")

        time.sleep(loop_sec)

if __name__ == "__main__":
    main()