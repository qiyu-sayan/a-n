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
    print("=== 0. 自检开始（Binance Demo 现货 + 合约） ===")

    demo_keys = {
        "apiKey": os.getenv("BINANCE_DEMO_API_KEY"),
        "secret": os.getenv("BINANCE_DEMO_API_SECRET"),
    }

    print("Demo Key 是否存在:", bool(demo_keys["apiKey"]))

    trader = Trader(
        exchange_id="binance",
        demo_keys=demo_keys,
        live_spot_keys={},
        live_futures_keys={},
    )

    # --- DEMO 现货：市价买 BTC/USDT ---
    print("=== 1. DEMO 现货市价买单 ===")
    spot_req = OrderRequest(
        env=Env.TEST,
        market=MarketType.SPOT,
        symbol="BTC/USDT",
        side=Side.BUY,
        amount=0.0001,        # 看你 demo 里余额，太大就减小
        price=None,
        leverage=None,
        position_side=None,
        reason="DEMO 现货下单验证",
    )
    spot_res = trader.place_order(spot_req)
    print("=== 现货结果 ===")
    print(Trader.format_wecom_message(spot_req, spot_res))

    # --- DEMO 合约：5x 杠杆 开多 BTCUSDT 永续 ---
    print("=== 2. DEMO 合约开多单 ===")
    futures_req = OrderRequest(
        env=Env.TEST,
        market=MarketType.FUTURES,
        symbol="BTC/USDT",     # defaultType=future 时，依然用 BTC/USDT
        side=Side.BUY,
        amount=0.001,          # 余额不多可以再调小
        price=None,
        leverage=5,
        position_side=PositionSide.LONG,
        reason="DEMO 合约开多验证",
    )
    futures_res = trader.place_order(futures_req)
    print("=== 合约结果 ===")
    print(Trader.format_wecom_message(futures_req, futures_res))

    print("=== 自检结束 ===")


if __name__ == "__main__":
    main()
