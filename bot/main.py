# 兼容两种运行方式：
# 1) python -m bot.main   （GitHub Actions / 包模式）
# 2) 直接在 bot 目录里 python main.py （本地快速测试）

try:
    # 包模式：repo 根目录执行 `python -m bot.main`
    from bot.trader import Trader
    from bot.strategy import Strategy, FUTURE_SYMBOLS
    from bot.wecom_notify import send_wecom_markdown
except ImportError:
    # 脚本模式：在 bot 目录下执行 `python main.py`
    from trader import Trader
    from strategy import Strategy, FUTURE_SYMBOLS
    from wecom_notify import send_wecom_markdown



def create_okx_exchanges():
    base_config = {
        "apiKey": os.getenv("OKX_PAPER_API_KEY"),
        "secret": os.getenv("OKX_PAPER_API_SECRET"),
        "password": os.getenv("OKX_PAPER_API_PASSPHRASE"),
        "enableRateLimit": True,
    }

    spot_ex = ccxt.okx({
        **base_config,
        "options": {"defaultType": "spot"}
    })

    fut_ex = ccxt.okx({
        **base_config,
        "options": {"defaultType": "swap", "defaultSettle": "usdt"}
    })

    return spot_ex, fut_ex


def fetch_candles(ex, symbol):
    try:
        return ex.fetch_ohlcv(symbol, timeframe="5m", limit=50)
    except Exception as e:
        print(f"[main] 拉 K 线失败 {symbol}: {e}")
        return []


def main_loop():
    spot_ex, fut_ex = create_okx_exchanges()
    trader = Trader(env="demo", spot_ex=spot_ex, fut_ex=fut_ex)
    strategy = Strategy(trader)

    send_wecom_markdown("🤖 交易机器人启动成功")

    while True:
        for symbol in FUTURE_SYMBOLS:
            candles = fetch_candles(fut_ex, symbol)
            if len(candles) < 20:
                continue

            orders = strategy.generate_orders(symbol, candles)

            for req in orders:
                print(f"[main] 下单: {req}")
                res = trader.place_order(req, market_type="futures")

        time.sleep(10)


if __name__ == "__main__":
    main_loop()
