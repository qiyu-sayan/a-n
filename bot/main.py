# bot/main.py

import os
import json
import time
from datetime import datetime, timezone

import ccxt  # 你 repo 里已经装过，如果不是 ccxt 而是 python-binance，再跟我说一声我们换一版

from bot.wecom_notify import wecom_notify, warn_451


# ========== 一些全局设置 ==========
# DEBUG: 是否强制测试下单（先开着验证一下，之后建议关掉）
FORCE_TEST_ORDER = True      # ✅ 现在先设成 True，确认能在测试网看到订单
FORCE_TEST_SYMBOL = "BTC/USDT"
FORCE_TEST_USDT = 10         # 每次测试单 10 USDT

PARAMS_PATH = os.path.join("config", "params.json")


# ========== 工具函数 ==========

def load_config(path: str):
    with open(path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    return cfg


def get_ccxt_client():
    """根据环境变量构建 ccxt binance 客户端（支持测试网 / 实盘切换）"""

    api_key = os.getenv("BINANCE_KEY")
    api_secret = os.getenv("BINANCE_SECRET")
    if not api_key or not api_secret:
        raise RuntimeError("缺少 BINANCE_KEY / BINANCE_SECRET 环境变量")

    use_testnet = os.getenv("BINANCE_TESTNET", "false").lower() == "true"

    options = {
        "apiKey": api_key,
        "secret": api_secret,
        "enableRateLimit": True,
        "options": {
            "defaultType": "spot",
        },
    }

    if use_testnet:
        # Spot Testnet 的专用 API 域名
        options["urls"] = {
            "api": {
                "public": "https://testnet.binance.vision/api",
                "private": "https://testnet.binance.vision/api",
            }
        }

    exchange = ccxt.binance(options)
    return exchange, use_testnet


def get_account_info(exchange: ccxt.binance):
    """获取账号资产（调试用）"""
    balance = exchange.fetch_balance()
    return balance


def calc_trade_amount_usdt(balance, max_usdt: float):
    """根据账户 USDT 余额和风控上限，算出这次最多能动用多少 USDT"""
    free_usdt = balance.get("free", {}).get("USDT", 0)
    return min(free_usdt, max_usdt)


def place_market_buy_usdt(exchange, symbol: str, usdt_amount: float):
    """用指定 USDT 金额市价买入 symbol（如 BTC/USDT）"""
    market = exchange.market(symbol)
    ticker = exchange.fetch_ticker(symbol)
    last_price = ticker["last"]

    # 按市价换算出数量，再根据交易所精度做裁剪
    amount = usdt_amount / last_price
    amount = float(exchange.amount_to_precision(symbol, amount))

    if amount <= 0:
        raise RuntimeError(f"计算出来的下单数量 <= 0，usdt={usdt_amount}, price={last_price}")

    order = exchange.create_market_buy_order(symbol, amount)
    return order


# ========== 你的策略逻辑占位 ==========
def run_strategy_and_get_signal(cfg, exchange):
    """
    这里本来应该是你真正的策略逻辑：
    - 拉历史 K 线
    - 计算因子 / 指标
    - 决定这次是 BUY / SELL / HOLD
    为了简单起见，先返回一个“永远不交易”的占位，下面我们用 FORCE_TEST_ORDER 去测试通路。
    """

    # 例子：正常情况下返回类似这样的结构：
    # return {
    #     "action": "buy",   # "buy" / "sell" / "hold"
    #     "symbol": "BTC/USDT",
    #     "usdt": 50,
    # }
    return {
        "action": "hold",
        "symbol": None,
        "usdt": 0,
    }


# ========== 主流程 ==========

def main():
    # 1. 加载配置
    cfg = load_config(PARAMS_PATH)

    enable_trading = os.getenv("ENABLE_TRADING", "false").lower() == "true"

    # 2. 初始化交易所
    exchange, use_testnet = get_ccxt_client()

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S %Z")
    print(f"[{now}] Bot started. testnet={use_testnet}, enable_trading={enable_trading}, FORCE_TEST_ORDER={FORCE_TEST_ORDER}")

    # 3. 打印一下账户资产，方便你在日志里确认真的连上了测试网
    try:
        balance = get_account_info(exchange)
        free_usdt = balance.get("free", {}).get("USDT", 0)
        print(f"[INFO] Free USDT: {free_usdt}")
    except Exception as e:
        warn_451(f"获取账户余额失败: {e}")
        raise

    # 4. 如果强制测试下单开关打开，直接在测试网市价买一点 BTC/USDT
    if FORCE_TEST_ORDER:
        if not enable_trading:
            print("[WARN] ENABLE_TRADING=false，虽然 FORCE_TEST_ORDER=True，但出于安全考虑不下单。")
        else:
            try:
                usdt_to_use = min(FOURCE_TEST_USDT := FORCE_TEST_USDT, float(free_usdt))
                if usdt_to_use <= 0:
                    print("[WARN] 账户 USDT 余额为 0，无法下测试单。")
                else:
                    print(f"[TEST] 在 {FORCE_TEST_SYMBOL} 上下测试买单，金额 {usdt_to_use} USDT ...")
                    order = place_market_buy_usdt(exchange, FORCE_TEST_SYMBOL, usdt_to_use)
                    msg = f"[TEST] Testnet 下单成功：{order}"
                    print(msg)
                    wecom_notify(msg)
            except Exception as e:
                msg = f"[TEST-ERROR] Testnet 下单失败：{e}"
                print(msg)
                warn_451(msg)

        # 测试模式下面的真实策略逻辑就先不跑了，直接 return。
        return

    # 5. 正常策略逻辑（FORCE_TEST_ORDER=False 时才会走到这里）
    signal = run_strategy_and_get_signal(cfg, exchange)
    print(f"[INFO] Strategy signal: {signal}")

    if signal["action"] == "hold":
        print("[INFO] 策略这次选择观望，不交易。")
        return

    if not enable_trading:
        print(f"[DRY-RUN] ENABLE_TRADING=false，只打印信号不下单：{signal}")
        return

    # 6. 按策略信号真正下单（这里以市价单为例）
    try:
        if signal["action"] == "buy":
            order = place_market_buy_usdt(exchange, signal["symbol"], signal["usdt"])
            msg = f"[TRADE] BUY {signal['symbol']} with {signal['usdt']} USDT, order={order}"
        elif signal["action"] == "sell":
            # 这里你可以再写个 place_market_sell_xxx，根据持仓数量来卖
            msg = "[TODO] 卖出逻辑还没写。"
            order = None
        else:
            msg = f"[WARN] 未知动作: {signal['action']}"
            order = None

        print(msg)
        wecom_notify(msg)

    except Exception as e:
        msg = f"[ERROR] 实际下单失败: {e}"
        print(msg)
        warn_451(msg)


if __name__ == "__main__":
    main()
