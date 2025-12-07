# bot/strategy.py
"""
混合策略系统（现货 + 合约 + 多周期）
-----------------------------------

这是一个专业级策略框架模板，用来构建你的自动交易系统。

特性：
- 同时支持：现货 + 合约
- 多周期分析：trend_tf 决定方向、entry_tf 决定入场
- 策略可组合（每个策略模块独立）
- 默认不下单（安全模式）
- 可以把任何策略逻辑（MA/RSI/结构/突破）插入到模板中

你未来的所有策略只需要在本文件内扩展。
主流程 main.py 与交易逻辑完全解耦。
"""

from __future__ import annotations

from typing import List, Optional, Dict, Any
import ccxt

from bot.trader import (
    Env,
    MarketType,
    Side,
    PositionSide,
    OrderRequest,
)

# =============================
# 参数区（策略全局控制中心）
# =============================

# 策略开关
ENABLE_SPOT = False
ENABLE_FUTURES = False

# 多周期
TREND_TF = "4h"   # 趋势方向周期
ENTRY_TF = "1h"   # 入场周期

# 要交易的币（可扩展多个）
SPOT_SYMBOLS = ["BTC/USDT"]
FUTURES_SYMBOLS = ["BTC/USDT:USDT"]


# =============================
# 工具函数：获取 K 线
# =============================

def fetch_ohlcv(exchange: ccxt.Exchange, symbol: str, timeframe: str, limit: int = 100):
    return exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)


def get_exchange():
    ex = ccxt.okx()
    ex.options["defaultType"] = "swap"  # 合约默认
    return ex


# =============================
# 主入口：外部调用 generate_orders(env)
# =============================

def generate_orders(env: Env) -> List[OrderRequest]:
    """
    返回本次全部订单（现货 + 合约），为空则 main.py 不会下单。
    """
    orders: List[OrderRequest] = []

    if ENABLE_SPOT:
        orders.extend(run_spot_strategy(env))

    if ENABLE_FUTURES:
        orders.extend(run_futures_strategy(env))

    return orders


# =============================
# 现货混合策略（模板）
# =============================

def run_spot_strategy(env: Env) -> List[OrderRequest]:
    exchange = get_exchange()
    results: List[OrderRequest] = []

    for symbol in SPOT_SYMBOLS:
        try:
            trend = fetch_ohlcv(exchange, symbol, TREND_TF)
            entry = fetch_ohlcv(exchange, symbol, ENTRY_TF)

            direction = detect_trend_direction(trend)
            entry_signal = detect_entry_signal(entry, direction)

            if entry_signal is None:
                continue

            order = OrderRequest(
                env=env,
                market=MarketType.SPOT,
                symbol=symbol,
                side=entry_signal,
                amount=0.001,
                price=None,
                leverage=None,
                position_side=None,
                reason=f"现货混合策略：趋势={direction}, 入场信号={entry_signal.value}",
            )
            results.append(order)

        except Exception as e:
            print(f"[SPOT] {symbol} 策略错误：{e}")

    return results


# =============================
# 合约混合策略（模板）
# =============================

def run_futures_strategy(env: Env) -> List[OrderRequest]:
    exchange = get_exchange()
    results: List[OrderRequest] = []

    for symbol in FUTURES_SYMBOLS:
        try:
            trend = fetch_ohlcv(exchange, symbol, TREND_TF)
            entry = fetch_ohlcv(exchange, symbol, ENTRY_TF)

            direction = detect_trend_direction(trend)
            entry_signal = detect_entry_signal(entry, direction)

            if entry_signal is None:
                continue

            order = OrderRequest(
                env=env,
                market=MarketType.FUTURES,
                symbol=symbol,
                side=entry_signal,
                amount=1,
                price=None,
                leverage=3,
                position_side=None,  # 单向模式
                reason=f"合约混合策略：趋势={direction}, 入场信号={entry_signal.value}",
            )
            results.append(order)

        except Exception as e:
            print(f"[FUTURES] {symbol} 策略错误：{e}")

    return results


# =============================
# 趋势方向检测（模板，可扩展）
# =============================

def detect_trend_direction(trend_kline: List[List[Any]]) -> str:
    """
    返回：
        "up"   → 看多趋势
        "down" → 看空趋势
        "flat" → 震荡，不做单
    默认使用 MA 指标做示例逻辑，但不会触发下单（因为 entry_signal 默认返回 None）
    """
    closes = [c[4] for c in trend_kline]

    # 示例 MA
    ma_fast = sum(closes[-5:]) / 5
    ma_slow = sum(closes[-20:]) / 20

    if ma_fast > ma_slow:
        return "up"
    elif ma_fast < ma_slow:
        return "down"
    return "flat"


# =============================
# 入场信号检测（模板，可扩展）
# =============================

def detect_entry_signal(entry_kline: List[List[Any]], trend: str) -> Optional[Side]:
    """
    返回：
        Side.BUY    → 开多
        Side.SELL   → 开空
        None        → 不下单

    模板逻辑：永远返回 None（安全）
    你未来可以自己加信号，例如：
        • 趋势为 up 且收出强势多头信号 → BUY
        • 趋势为 down 且收盘跌破区间 → SELL
    """
    return None
