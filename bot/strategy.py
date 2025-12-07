# bot/strategy.py
"""
策略模块（你可以在这里组合多种策略）

目标：
- 同时支持现货 & 合约（你在问题 1 里选了 C）
- 预留 K 线策略 / 你自己的旧策略 / 手工测试单的组合空间
- 默认行为：不下单（保证安全）

核心对外接口：
    generate_orders(env: Env) -> list[OrderRequest]

后续你想改策略，只需要改这个文件，不需要动 main.py / trader.py。
"""

from __future__ import annotations

from typing import List

from bot.trader import (
    Env,
    MarketType,
    Side,
    PositionSide,
    OrderRequest,
)

# ===================== 策略开关（很重要） =====================

# 你可以按需要打开/关闭不同策略模块
ENABLE_SPOT_STRATEGY = False      # 现货 K 线策略
ENABLE_FUTURES_STRATEGY = False   # 合约 K 线 / 多空策略
ENABLE_MANUAL_TEST = False        # 手工测试单 / 旧逻辑迁移区


# ===================== 对外主入口 =====================

def generate_orders(env: Env) -> List[OrderRequest]:
    """
    生成本次要执行的所有订单（现货 + 合约混合）。

    当前默认逻辑：
        - 所有策略开关默认是 False，所以返回 []（不下单）
        - 你可以逐个把开关改成 True，调试各自的策略模块。
    """
    orders: List[OrderRequest] = []

    if ENABLE_SPOT_STRATEGY:
        orders.extend(_spot_kline_strategy(env))

    if ENABLE_FUTURES_STRATEGY:
        orders.extend(_futures_kline_strategy(env))

    if ENABLE_MANUAL_TEST:
        orders.extend(_manual_or_legacy_strategy(env))

    return orders


# ===================== 现货 K 线策略（占位） =====================

def _spot_kline_strategy(env: Env) -> List[OrderRequest]:
    """
    这里预留给“现货 K 线策略”，例如：
        - MA5 上穿 MA20 买入
        - RSI 超卖反弹买入
        - etc.

    目前只是模板，默认不生成任何订单。
    你以后可以在这里用 ccxt.okx().fetch_ohlcv(...) 拉 K 线，然后生成 OrderRequest。
    """
    signals: List[OrderRequest] = []

    # 示例（伪代码，默认注释掉）：
    #
    # import ccxt
    # okx = ccxt.okx()
    # ohlcv = okx.fetch_ohlcv("BTC/USDT", timeframe="1h", limit=100)
    # ... 计算策略 ...
    # if 出现买入信号:
    #     signals.append(
    #         OrderRequest(
    #             env=env,
    #             market=MarketType.SPOT,
    #             symbol="BTC/USDT",
    #             side=Side.BUY,
    #             amount=0.001,
    #             price=None,
    #             leverage=None,
    #             position_side=None,
    #             reason="现货策略信号：XXX",
    #         )
    #     )

    return signals


# ===================== 合约 K 线 / 多空策略（占位） =====================

def _futures_kline_strategy(env: Env) -> List[OrderRequest]:
    """
    这里预留给“合约策略”，例如：
        - 趋势跟随：突破区间上沿开多，跌破下沿开空
        - HL/HH 结构判断多空
        - etc.

    目前也是模板，默认不生成任何订单。
    """
    signals: List[OrderRequest] = []

    # 示例（伪代码）：
    #
    # import ccxt
    # okx = ccxt.okx()
    # ohlcv = okx.fetch_ohlcv("BTC/USDT:USDT", timeframe="4h", limit=200, params={"instType": "SWAP"})
    # ... 计算多空方向 ...
    # if 看多:
    #     signals.append(
    #         OrderRequest(
    #             env=env,
#             market=MarketType.FUTURES,
#             symbol="BTC/USDT:USDT",
#             side=Side.BUY,
#             amount=1,
#             price=None,
#             leverage=5,
#             position_side=None,  # 当前使用单向持仓
#             reason="合约策略信号：看多",
#         )
#     )
# elif 看空:
#     signals.append(
#         OrderRequest(
#             env=env,
#             market=MarketType.FUTURES,
#             symbol="BTC/USDT:USDT",
#             side=Side.SELL,
#             amount=1,
#             price=None,
#             leverage=5,
#             position_side=None,
#             reason="合约策略信号：看空",
#         )
#     )

    return signals


# ===================== 手工 / 旧逻辑策略（占位） =====================

def _manual_or_legacy_strategy(env: Env) -> List[OrderRequest]:
    """
    这里是给你：
        - 手工测试单
        - 以前 main_old.py / 老 strategy 的迁移逻辑

    你可以在这里硬编码一些订单，方便调试整条链路。
    """
    signals: List[OrderRequest] = []

    # 示例：如果你想测试一下“每次跑都在 DEMO 合约开 1 张多单”，
    # 只需要把 ENABLE_MANUAL_TEST 改成 True，然后取消下面注释。
    #
    # signals.append(
    #     OrderRequest(
    #         env=env,
    #         market=MarketType.FUTURES,
    #         symbol="BTC/USDT:USDT",
    #         side=Side.BUY,
    #         amount=1,
    #         price=None,
    #         leverage=3,
    #         position_side=None,
    #         reason="手工测试单：DEMO 合约开多 1 张",
    #     )
    # )

    return signals
