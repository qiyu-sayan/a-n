import os
import time
from binance.client import Client
from binance.exceptions import BinanceAPIException

# === 读取环境变量 ===
BINANCE_KEY = os.getenv("BINANCE_KEY")
BINANCE_SECRET = os.getenv("BINANCE_SECRET")

ENABLE_TRADING = True                # 启用真实下单（demo盘是真下单）
PAPER_TRADING = False                # 纸面模式（只打印，不下单）

TRADE_SYMBOLS = ["BTCUSDT", "ETHUSDT"]
TRADE_AMOUNT_USDT = 10               # 每笔下单金额

# WeCom 通知 (没填就跳过)
WECHAT_WEBHOOK = os.getenv("WECHAT_WEBHOOK", "")


# ========== 简易通知 ==========
def wecom_notify(msg):
    if not WECHAT_WEBHOOK:
        print("[wecom] WECHAT_WEBHOOK 未配置，跳过发送：", msg)
        return
    try:
        import requests
        requests.post(WECHAT_WEBHOOK, json={"msgtype": "text", "text": {"content": msg}})
    except Exception as e:
        print("[wecom] 发送失败:", e)


# ========== 获取 Binance Demo Client ==========
def make_client():
    if not BINANCE_KEY or not BINANCE_SECRET:
        raise RuntimeError("❌ BINANCE KEY/SECRET 未设置")

    print("🔧 使用 Binance Demo 环境（demo.binance.com）")
    client = Client(
        api_key=BINANCE_KEY,
        api_secret=BINANCE_SECRET,
        demo=True     # ⬅⬅ 重点！一定是 demo=True
    )
    return client


# ========== 下单 ==========
def place_order(client, symbol):
    print(f"\n=== 处理交易对: {symbol} ===")

    try:
        # 最新价格
        ticker = client.get_symbol_ticker(symbol=symbol)
        price = float(ticker["price"])

        quantity = round(TRADE_AMOUNT_USDT / price, 6)

        if PAPER_TRADING:
            print(f"📝 [纸面交易] {symbol} 市价买入数量: {quantity}")
            return {"status": "paper"}

        if ENABLE_TRADING:
            print(f"📈 下单: {symbol} 数量 {quantity}")
            order = client.order_market_buy(
                symbol=symbol,
                quantity=quantity
            )
            print("✅ 下单成功:", order)
            return order

    except BinanceAPIException as e:
        print(f"❌ 下单失败 ({symbol}) - binance: {e.status_code}, msg: {e.message}")
        return {"error": str(e)}

    except Exception as e:
        print(f"❌ 未知错误 ({symbol}):", e)
        return {"error": str(e)}


# ========== 入口函数 ==========
def run_bot():
    print("🚀 Bot 开始运行")
    print("环境: DEMO(模拟盘)")
    print("ENABLE_TRADING:", ENABLE_TRADING)
    print("PAPER_TRADING:", PAPER_TRADING)
    print("每笔下单 USDT:", TRADE_AMOUNT_USDT)
    print("交易标的:", ", ".join(TRADE_SYMBOLS))

    client = make_client()
    results = {}

    for symbol in TRADE_SYMBOLS:
        result = place_order(client, symbol)
        results[symbol] = result
        time.sleep(1)

    print("\n📊 本次运行结果：")
    for s, r in results.items():
        print(s, "→", r)

    wecom_notify(f"run-bot 执行结束:\n{results}")


if __name__ == "__main__":
    run_bot()
