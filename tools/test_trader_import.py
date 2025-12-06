# tools/test_trader_import.py

import os

from bot.trader import (
    Trader,
    Env,
    MarketType,
    Side,
    PositionSide,
    OrderRequest,
)


def main():
    print("=== 0. 自检开始 ===")

    # === 1. Load Spot Test Keys ===
    print("=== 1. 加载现货 TEST API Key ===")
    spot_test_keys = {
        "apiKey": os.getenv("SPOT_TEST_API_KEY"),
        "secret": os.getenv("SPOT_TEST_API_SECRET"),
    }

    print("现货 TEST Key 是否存在:", bool(spot_test_keys["apiKey"]))

    # === 2. Load Futures Test Keys ===
    print("=== 2. 加载合约 TEST API Key ===")
    futures_test_keys = {
        "apiKey": os.getenv("FUTURES_TEST_API_KEY"),
        "secret": os.getenv("FUTURES_TEST_API_SECRET"),
    }

    print("合约 TEST Key 是否存在:", bool(futures_test_keys["apiKey"]))

    # LIVE 暂不使用（给空字典）
    empty_keys = {}

    # 创建交易实例
    print("=== 3. 初始化 Trader (Binance) ===")
    trader = Trader(
        exchange_id="binance",
        spot_test_keys=spot_test_keys,
        spot_live_keys=empty_keys,
        futures_test_keys=futures_test_keys,
        futures_live_keys=empty_keys,
    )

    # === 4. 构造一笔现货市价买单（TEST）===
    print("=== 4. 构造 TEST 现货市价买单 ===")
    symbol = "BTC/USDT"
    amount = 0.0001   # 小额测试单

    req = OrderRequest(
        env=Env.TEST,
        market=MarketType.SPOT,
        symbol=symbol,
        side=Side.BUY,
        amount=amount,
        price=None,
        leverage=None,
        position_side=None,
        reason="TEST 现货下单验证",
    )

    # === 5. 下单 ===
    print("=== 5. 尝试下单 ===")
    res = trader.place_order(req)

    # === 6. 输出订单结果 ===
    print("=== 6. 下单结果 ===")
    msg = Trader.format_wecom_message(req, res)
    print(msg)

    print("=== 自检结束 ===")


if __name__ == "__main__":
    main()
