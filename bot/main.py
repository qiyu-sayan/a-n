import os
import sys
import time
import hmac
import json
import hashlib
import traceback
from datetime import datetime, timezone
from typing import List, Tuple, Dict

import requests
from urllib.parse import urlencode


# ====== 配置相关工具函数 ======

def env_bool(name: str, default: bool = False) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "y", "on")


def load_params() -> Dict:
    """
    尝试从 config/params.json 读取参数，
    读不到就用内置默认值。
    """
    params = {
        "symbols": ["BTCUSDT", "ETHUSDT"],
        "order_usdt": 10.0,     # 每笔用多少 USDT 下单
        "paper": False,         # 纸上仿真（只打印不下单）
    }

    path = os.path.join("config", "params.json")
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)

            if "symbols" in data:
                if isinstance(data["symbols"], list):
                    params["symbols"] = [str(s).upper() for s in data["symbols"]]
                elif isinstance(data["symbols"], str):
                    params["symbols"] = [
                        s.strip().upper()
                        for s in data["symbols"].split(",")
                        if s.strip()
                    ]

            if "order_usdt" in data:
                try:
                    params["order_usdt"] = float(data["order_usdt"])
                except Exception:
                    pass

            if "paper" in data:
                params["paper"] = bool(data["paper"])
    except Exception:
        # 配置读失败不致命，直接用默认
        print("[WARN] 读取 config/params.json 失败，使用内置默认参数", file=sys.stderr)

    # 环境变量覆盖（方便你以后在 workflow 里调）
    symbols_env = os.getenv("SYMBOLS")
    if symbols_env:
        params["symbols"] = [
            s.strip().upper() for s in symbols_env.split(",") if s.strip()
        ]

    order_env = os.getenv("ORDER_USDT")
    if order_env:
        try:
            params["order_usdt"] = float(order_env)
        except Exception:
            pass

    paper_env = os.getenv("PAPER")
    if paper_env is not None:
        params["paper"] = env_bool("PAPER", params["paper"])

    return params


# ====== Binance HTTP 封装 ======

API_BASE_MAIN = "https://api.binance.com"
API_BASE_TESTNET = "https://testnet.binance.vision"


class BinanceClient:
    def __init__(self, api_key: str, api_secret: str, testnet: bool = True):
        self.api_key = api_key
        self.api_secret = api_secret
        self.base_url = API_BASE_TESTNET if testnet else API_BASE_MAIN

    # 公共 GET 请求（无需签名）
    def public_get(self, path: str, params: Dict = None) -> Tuple[int, Dict]:
        url = self.base_url + path
        resp = requests.get(url, params=params or {}, timeout=10)
        try:
            data = resp.json()
        except Exception:
            data = {"raw": resp.text}
        return resp.status_code, data

    # 带签名请求
    def signed_request(
        self, method: str, path: str, params: Dict = None
    ) -> Tuple[int, Dict]:
        if params is None:
            params = {}

        params["timestamp"] = int(time.time() * 1000)
        # 可以适当放宽 recvWindow
        params.setdefault("recvWindow", 5000)

        query_string = urlencode(params, doseq=True)
        signature = hmac.new(
            self.api_secret.encode("utf-8"),
            query_string.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        query_with_sig = f"{query_string}&signature={signature}"

        url = self.base_url + path + "?" + query_with_sig
        headers = {"X-MBX-APIKEY": self.api_key}

        resp = requests.request(method, url, headers=headers, timeout=10)
        try:
            data = resp.json()
        except Exception:
            data = {"raw": resp.text}
        return resp.status_code, data

    # 简单市价买单（按 quoteOrderQty 下单：用多少 USDT 买）
    def market_buy_quote(self, symbol: str, quote_usdt: float) -> Tuple[int, Dict]:
        params = {
            "symbol": symbol,
            "side": "BUY",
            "type": "MARKET",
            "quoteOrderQty": str(quote_usdt),
        }
        return self.signed_request("POST", "/api/v3/order", params)


# ====== 交易逻辑（非常简单：每个 symbol 市价买一笔） ======

def run_bot():
    now = datetime.now(timezone.utc).astimezone()
    params = load_params()

    api_key = os.getenv("BINANCE_KEY")
    api_secret = os.getenv("BINANCE_SECRET")

    if not api_key or not api_secret:
        print("❌ 缺少 BINANCE_KEY / BINANCE_SECRET 环境变量，无法下单")
        return

    # 是否使用 TESTNET（模拟盘）
    is_testnet = env_bool("TESTNET", True)

    # 是否真正下单（False 就只打印）
    enable_trading = env_bool("ENABLE_TRADING", True)

    symbols: List[str] = params["symbols"]
    order_usdt: float = params["order_usdt"]
    paper: bool = params["paper"]

    print("📌 Bot 开始运行")
    print(f"时间: {now.strftime('%Y-%m-%d %H:%M:%S%z')}")
    print(f"环境: {'TESTNET(模拟盘)' if is_testnet else 'LIVE(实盘)'}")
    print(f"ENABLE TRADING: {enable_trading}")
    print(f"PAPER(纸上仿真): {paper}")
    print(f"每笔下单 USDT: {order_usdt}")
    print(f"交易标的: {', '.join(symbols)}")
    print("-" * 60)

    client = BinanceClient(api_key, api_secret, testnet=is_testnet)

    results = []

    for symbol in symbols:
        print(f"\n=== 处理交易对: {symbol} ===")

        # 先测试一下这个 symbol 是否在当前环境可用
        code, info = client.public_get("/api/v3/exchangeInfo", {"symbol": symbol})
        if code != 200:
            print(
                f"{symbol}: 获取交易所信息失败 code={code}, resp={info}. "
                f"请检查：1) 是否 TESTNET 里存在该交易对；2) API key 环境是否匹配。"
            )
            results.append((symbol, False, info))
            continue

        if not enable_trading or paper:
            print(
                f"{symbol}: 当前为 {'PAPER 模式' if paper else 'ENABLE_TRADING=False'}，"
                f"只打印，不实际下单。"
            )
            results.append((symbol, True, {"msg": "dry-run"}))
            continue

        try:
            code, resp = client.market_buy_quote(symbol, order_usdt)
            if code == 200:
                print(f"{symbol}: ✅ 下单成功，订单返回: {resp}")
                results.append((symbol, True, resp))
            else:
                print(f"{symbol}: ❌ 下单失败，code={code}, resp={resp}")
                results.append((symbol, False, resp))
        except Exception as e:
            print(f"{symbol}: ❌ 下单异常: {e}")
            traceback.print_exc()
            results.append((symbol, False, {"exception": str(e)}))

        # 防止过于频繁
        time.sleep(0.5)

    print("\n本次运行结果:")
    for symbol, ok, detail in results:
        status = "成功" if ok else "失败"
        print(f"{symbol}: 下单{status} - {detail}")

    print("\n✅ 本次 run-bot 任务执行完毕")


def main():
    try:
        run_bot()
    except Exception as e:
        # 这里兜底，防止抛异常导致整个 workflow 变红
        print("❌ Bot 运行过程中出现未捕获异常:", e, file=sys.stderr)
        traceback.print_exc()


if __name__ == "__main__":
    main()
