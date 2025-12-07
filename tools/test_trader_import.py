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


def main() -> None:
    print("=== 0. 自检开始（OKX 模拟盘：现货 + 合约） ===")

    paper_keys = {
        "apiKey": os.getenv("OKX_PAPER_API_KEY"),
        "secret": os.getenv("OKX_PAPER_API_SECRET"),
        "password": os.getenv("OKX_PAPER_API_PASSPHRASE"),
    }

    live_keys = {
        "apiKey": os.getenv("OKX_LIVE_API_KEY"),
        "secret": os.getenv("OKX_LIVE_API_SECRET"),
        "password": os.getenv("OKX_LIVE_API_PASSPHRASE"),
    }

    print("Paper Key 是否存在:", bool(paper_keys["apiKey"]))
    print("Live Key 是否存在:", bool(live_keys["apiKey"]))

    trader = Trader(
        exchange_id="okx",
        paper_keys=paper_keys,
        live_keys=live_keys,
    )

    # === 1. 模拟盘现货市价买单 ===
    print("=== 1. DEMO 现货市价买单 ===")
    spot_req = OrderRequest(
        env=Env.TEST,
        market=MarketType.SPOT,
        symbol="BTC/USDT",   # OKX 现货交易对
        side=Side.BUY,
        amount=0.001,        # 根据模拟盘余额自行调整
        price=None,          # 市价
        leverage=None,
        position_side=None,
        reason="DEMO 现货下单验证",
    )
    spot_res = trader.place_order(spot_req)
    print("=== 现货结果 ===")
    print(Trader.format_wecom_message(spot_req, spot_res))

    # === 2. 模拟盘合约开多单（USDT 永续） ===
    print("=== 2. DEMO 合约开多单 ===")
    futures_req = OrderRequest(
        env=Env.TEST,
        market=MarketType.FUTURES,
        symbol="BTC/USDT:USDT",     # OKX U 本位永续
        side=Side.BUY,
        amount=1,                   # 合约张数，余额不多可减小
        price=None,                 # 市价
        leverage=5,
        position_side=None,         # 单向持仓模式，不再传 posSide
        reason="DEMO 合约开多验证",
    )

    futures_res = trader.place_order(futures_req)
    print("=== 合约结果 ===")
    print(Trader.format_wecom_message(futures_req, futures_res))

    print("=== 自检结束 ===")


if __name__ == "__main__":
    main()
