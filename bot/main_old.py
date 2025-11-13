# -*- coding: utf-8 -*-
import os
import time
import json
import traceback
import requests
from requests import RequestException
from bot.wecom_notify import wecom_notify, wrap_run, warn_451
from bot.strategy import load_params, route_signal

# ===================== 读取配置 =====================
# 优先从 config/params.json 读取，兼容 Secrets 环境变量
PARAMS = load_params()

BINANCE_KEY = os.getenv("BINANCE_KEY", "")
BINANCE_SECRET = os.getenv("BINANCE_SECRET", "")
TESTNET = os.getenv("BINANCE_TESTNET", "false").lower() in {"1", "true", "yes"}

# 从 params.json 读取主策略参数
MODE = PARAMS.get("mode", "paper").lower()       # live / paper
SYMBOLS = PARAMS.get("symbols", ["BTCUSDT"])
INTERVAL = PARAMS.get("interval", "1m")
ORDER_USDT = float(PARAMS.get("order_usdt", 10))
STRATEGY = PARAMS.get("strategy", "sma_rsi")
STRAT_PARAMS = PARAMS.get("params", {})
RISK = PARAMS.get("risk", {})
ENABLE_TRADING = MODE == "live"
PAPER = MODE != "live"

# API base
REST_BASE = "https://testnet.binance.vision" if TESTNET else "https://api.binance.com"


# ===================== 通用请求 =====================
def http_get(url, headers=None, timeout=15):
    try:
        r = requests.get(url, headers=headers, timeout=timeout)
        if r.status_code == 451:
            warn_451(url)
            return None
        r.raise_for_status()
        return r.json()
    except Exception as e:
        wecom_notify(f"⚠️ GET请求失败：{e}\n{url}")
        return None


def http_post(url, headers=None, data=None, timeout=15):
    try:
        r = requests.post(url, headers=headers, data=data, timeout=timeout)
        if r.status_code == 451:
            warn_451(url)
            return None
        r.raise_for_status()
        return r.json()
    except requests.HTTPError as he:
        try:
            text = he.response.text
        except Exception:
            text = ""
        wecom_notify(f"⚠️ POST失败：{he}\nData: {data}\nResp: {text}")
        return None
    except Exception as e:
        wecom_notify(f"⚠️ POST请求异常：{e}\n{url}")
        return None


# ===================== 账户与交易 =====================
def sign_params(params: dict, secret: str) -> str:
    import hmac, hashlib
    from urllib.parse import urlencode
    query = urlencode(params, doseq=True)
    sig = hmac.new(secret.encode(), query.encode(), hashlib.sha256).hexdigest()
    return f"{query}&signature={sig}"


def private_headers():
    return {"X-MBX-APIKEY": BINANCE_KEY}


def ts():
    return int(time.time() * 1000)


def get_account_info():
    url = f"{REST_BASE}/api/v3/account"
    params = {"timestamp": ts(), "recvWindow": 60000}
    q = sign_params(params, BINANCE_SECRET)
    return http_get(f"{url}?{q}", headers=private_headers())


def get_balance(asset: str) -> float:
    info = get_account_info()
    if not info or "balances" not in info:
        return 0.0
    for b in info["balances"]:
        if b["asset"] == asset:
            try:
                return float(b.get("free", "0"))
            except:
                return 0.0
    return 0.0


def place_market_order(symbol: str, side: str, quote_usdt: float = None, quantity: float = None):
    """
    市价下单（实盘 / 纸交易 自动区分）
    """
    side = side.upper()
    if PAPER or not ENABLE_TRADING:
        wecom_notify(f"🧪 纸交易 {side} {symbol}（金额: {quote_usdt or quantity}）")
        return {"paper": True, "symbol": symbol, "side": side}

    endpoint = f"{REST_BASE}/api/v3/order"
    params = {
        "symbol": symbol,
        "side": side,
        "type": "MARKET",
        "timestamp": ts(),
        "recvWindow": 60000,
    }
    if side == "BUY":
        params["quoteOrderQty"] = str(quote_usdt)
    else:
        params["quantity"] = str(quantity)

    data = sign_params(params, BINANCE_SECRET)
    res = http_post(endpoint, headers=private_headers(), data=data)
    if res:
        wecom_notify(f"✅ 成功下单：{side} {symbol}\n{json.dumps(res, ensure_ascii=False)}")
    return res


# ===================== 行情与指标 =====================
def fetch_klines(symbol: str, interval: str = "1m", limit: int = 200):
    url = f"{REST_BASE}/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}"
    res = http_get(url)
    return res if isinstance(res, list) else []


def last_closes(klines):
    closes = []
    for k in klines:
        try:
            closes.append(float(k[4]))
        except:
            pass
    return closes


# ===================== 主策略逻辑 =====================
def trade_symbol(symbol: str):
    klines = fetch_klines(symbol, INTERVAL, 200)
    if not klines:
        wecom_notify(f"⚠️ {symbol} 无法获取K线数据")
        return

    closes = last_closes(klines)
    if len(closes) < 30:
        return

    signal = route_signal(STRATEGY, closes, STRAT_PARAMS)
    price = closes[-1]

    if signal == "BUY":
        place_market_order(symbol, "BUY", quote_usdt=ORDER_USDT)
    elif signal == "SELL":
        base = symbol.replace("USDT", "")
        bal = get_balance(base)
        if bal > 0:
            place_market_order(symbol, "SELL", quantity=bal)
        else:
            wecom_notify(f"ℹ️ {symbol} 信号 SELL，但余额不足。")
    else:
        print(f"{symbol}: HOLD @ {price}")


# ===================== 主流程 =====================
def main():
    mode_name = "实盘" if ENABLE_TRADING and not PAPER else "纸交易"
    wecom_notify(f"🚀 启动 {mode_name} 模式\n策略: {STRATEGY}\n交易对: {', '.join(SYMBOLS)}\n下单金额: {ORDER_USDT} USDT")

    for s in SYMBOLS:
        try:
            trade_symbol(s)
            time.sleep(1)
        except Exception as e:
            wecom_notify(f"❌ {s} 运行异常：{e}\n{traceback.format_exc()}")

    wecom_notify("✅ 本轮执行完成")


# ===================== 启动包装 =====================
if __name__ == "__main__":
    wrap_run(main)