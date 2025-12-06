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

    # === 1. 加载 TEST API Key ===
    print("=== 1. 加载现货 TEST API Key ===")
    spot_test_keys = {
        "apiKey": os.getenv("SPOT_TEST_API_KEY"),
        "secret": os.getenv("SPOT_TEST_API_SECRET"),
    }
    print("现货 TEST Key 是否存在:", bool(spot_test_keys["apiKey"]))

    print("=== 2. 加载合约 TEST API Key ===")
    futures_test_keys = {
        "apiKey": os.getenv("FUTURES_TEST_API_KEY"),
        "secret": os.getenv("FUTURES_TEST_API_SECRET"),
    }
    print("合约 TEST Key 是否存在:", bool(futures_test_keys["apiKey"]))

    empty_keys = {}  # LIVE 暂时不用

    # === 3. 初始化 Trader ===
    print("=== 3. 初始化 Trader (Binance) ===")
    trader = Trader(
        exchange_id="binance",
        spot_test_keys=spot_test_keys,
        spot_live_keys=empty_keys,
        futures_test_keys=futures_test_keys,
        futures_live_keys=empty_keys,
    )

    # === 4. TEST 现货市价买单 ===
    print("=== 4. 构造 TEST 现货市价买单 ===")
    spot_symbol = "BTC/USDT"
    spot_amount = 0.0001  # 小额测试

    spot_req = OrderRequest(
        env=Env.TEST,
        market=MarketType.SPOT,
        symbol=spot_symbol,
        side=Side.BUY,
        amount=spot_amount,
        price=None,          # 市价
        leverage=None,
        position_side=None,
        reason="TEST 现货下单验证",
    )

    print("=== 5. 发送现货订单 ===")
    spot_res = trader.place_order(spot_req)

    print("=== 6. 现货下单结果 ===")
    spot_msg = Trader.format_wecom_message(spot_req, spot_res)
    print(spot_msg)

    # === 7. TEST 合约开多单（USDT-M 永续）===
    print("=== 7. 构造 TEST 合约开多单 ===")
    # 对于 ccxt 的 binance 线性永续，一般符号是 "BTC/USDT:USDT"
    futures_symbol = "BTC/USDT:USDT"
    futures_amount = 0.001   # 合约张数 / 数量（根据账户余额酌情调小点）
    futures_leverage = 5

    futures_req = OrderRequest(
        env=Env.TEST,
        market=MarketType.FUTURES,
        symbol=futures_symbol,
        side=Side.BUY,                 # buy
        amount=futures_amount,
        price=None,                    # 市价
        leverage=futures_leverage,
        position_side=PositionSide.LONG,  # 开多
        reason="TEST 合约开多验证",
    )

    print("=== 8. 发送合约订单 ===")
    futures_res = trader.place_order(futures_req)

    print("=== 9. 合约下单结果 ===")
    futures_msg = Trader.format_wecom_message(futures_req, futures_res)
    print(futures_msg)

    print("=== 自检结束 ===")


if __name__ == "__main__":
    main()
