# bot/strategy.py
"""
混合策略系统（现货 + 合约 + 多周期）

当前版本：
- 只在 Env.TEST（OKX 模拟盘）下单
- Env.LIVE（实盘）直接返回空列表
- 同时启用现货 & 合约策略
- 4h MA5/MA20 判趋势 + 强度过滤
- 1h 动量入场 + 最小波动过滤
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
# 全局开关 & 参数
# =============================

ENABLE_SPOT = True
ENABLE_FUTURES = True

TREND_TF = "4h"
ENTRY_TF = "1h"

SPOT_SYMBOLS = ["BTC/USDT"]
FUTURES_SYMBOLS = ["BTC/USDT:USDT"]

# 趋势强度：MA5 与 MA20 至少相差 0.1%
MIN_TREND_STRENGTH = 0.001  # 0.1%

# 入场最小波动：1h 收盘至少波动 0.1%
MIN_ENTRY_MOVE = 0.001      # 0.1%


# =============================
# ccxt 工具
# =============================

def get_exchange_spot() -> ccxt.Exchange:
    ex = ccxt.okx()
    ex.options["defaultType"] = "spot"
    return ex


def get_exchange_futures() -> ccxt.Exchange:
    ex = ccxt.okx()
    ex.options["defaultType"] = "swap"  # 永续
    return ex


def fetch_ohlcv(
    exchange: ccxt.Exchange,
    symbol: str,
    timeframe: str,
    limit: int = 100,
    params: Optional[Dict[str, Any]] = None,
):
    return exchange.fetch_ohlcv(
        symbol,
        timeframe=timeframe,
        limit=limit,
        params=params or {},
    )


# =============================
# 对外主入口
# =============================

def generate_orders(env: Env) -> List[OrderRequest]:
    # 实盘继续锁死
    if env == Env.LIVE:
        print("[strategy] LIVE 环境暂未启用策略，返回空列表。")
        return []

    orders: List[OrderRequest] = []

    if ENABLE_SPOT:
        orders.extend(run_spot_strategy(env))

    if ENABLE_FUTURES:
        orders.extend(run_futures_strategy(env))

    return orders


# =============================
# 现货策略
# =============================

def run_spot_strategy(env: Env) -> List[OrderRequest]:
    ex = get_exchange_spot()
    results: List[OrderRequest] = []

    for symbol in SPOT_SYMBOLS:
        try:
            trend = fetch_ohlcv(ex, symbol, TREND_TF, limit=50, params={"instType": "SPOT"})
            entry = fetch_ohlcv(ex, symbol, ENTRY_TF, limit=50, params={"instType": "SPOT"})

            direction = detect_trend_direction(trend)
            side = detect_entry_signal(entry, direction)

            if side is None or direction == "flat":
                continue

            order = OrderRequest(
                env=env,
                market=MarketType.SPOT,
                symbol=symbol,
                side=side,
                amount=0.001,
                price=None,
                leverage=None,
                position_side=None,
                reason=f"现货混合策略: trend={direction}, signal={side.value}",
            )
            results.append(order)

        except Exception as e:
            print(f"[SPOT] {symbol} 策略错误: {e}")

    return results


# =============================
# 合约策略
# =============================

def run_futures_strategy(env: Env) -> List[OrderRequest]:
    ex = get_exchange_futures()
    results: List[OrderRequest] = []

    for symbol in FUTURES_SYMBOLS:
        try:
            trend = fetch_ohlcv(ex, symbol, TREND_TF, limit=50, params={"instType": "SWAP"})
            entry = fetch_ohlcv(ex, symbol, ENTRY_TF, limit=50, params={"instType": "SWAP"})

            direction = detect_trend_direction(trend)
            side = detect_entry_signal(entry, direction)

            if side is None or direction == "flat":
                continue

            order = OrderRequest(
                env=env,
                market=MarketType.FUTURES,
                symbol=symbol,
                side=side,
                amount=1,           # 1 张，配合 3x 杠杆
                price=None,
                leverage=3,
                position_side=None,
                reason=f"合约混合策略: trend={direction}, signal={side.value}",
            )
            results.append(order)

        except Exception as e:
            print(f"[FUTURES] {symbol} 策略错误: {e}")

    return results


# =============================
# 趋势判断：MA5 / MA20 + 强度过滤
# =============================

def detect_trend_direction(trend_kline: List[List[Any]]) -> str:
    closes = [c[4] for c in trend_kline]
    if len(closes) < 20:
        return "flat"

    ma_fast = sum(closes[-5:]) / 5
    ma_slow = sum(closes[-20:]) / 20

    if ma_slow == 0:
        return "flat"

    diff_ratio = abs(ma_fast - ma_slow) / ma_slow

    if diff_ratio < MIN_TREND_STRENGTH:
        # 趋势太弱，当作震荡
        return "flat"

    if ma_fast > ma_slow:
        return "up"
    elif ma_fast < ma_slow:
        return "down"
    else:
        return "flat"


# =============================
# 入场信号：1h 动量 + 最小波动过滤
# =============================

def detect_entry_signal(entry_kline: List[List[Any]], trend: str) -> Optional[Side]:
    if trend not in ("up", "down"):
        return None

    closes = [c[4] for c in entry_kline]
    if len(closes) < 3:
        return None

    last = closes[-1]
    prev = closes[-2]

    if prev == 0:
        return None

    move_ratio = abs(last - prev) / prev
    if move_ratio < MIN_ENTRY_MOVE:
        # 波动太小，不值得交易
        return None

    if trend == "up" and last > prev:
        return Side.BUY
    if trend == "down" and last < prev:
        return Side.SELL

    return None
