# bot/strategy.py
"""
混合策略系统（现货 + 合约 + 多周期）

当前版本：
- 只在 Env.TEST（OKX 模拟盘）下单
- Env.LIVE（实盘）直接返回空列表（安全锁）
- 启用现货 & 合约策略
- 4h MA5/MA20 判趋势 + 强度
- 1h 动量入场 + 最小波动过滤
- 合约根据趋势强度 + 入场动量动态调整杠杆（1x ~ 20x）
"""

from __future__ import annotations

from typing import List, Optional, Dict, Any, Tuple
import ccxt

from bot.trader import (
    Env,
    MarketType,
    Side,
    OrderRequest,
)

# =============================
# 全局开关 & 参数
# =============================

ENABLE_SPOT = False
ENABLE_FUTURES = True

TREND_TF = "4h"
ENTRY_TF = "1h"

SPOT_SYMBOLS = ["BTC/USDT", "ETH/USDT"]
FUTURES_SYMBOLS = ["BTC/USDT:USDT", "ETH/USDT:USDT"]


# 趋势强度：MA5 与 MA20 至少相差 0.1%
MIN_TREND_STRENGTH = 0.001  # 0.1%

# 入场最小波动：1h 收盘至少波动 0.1%
MIN_ENTRY_MOVE = 0.001      # 0.1%

# 杠杆范围（主要用于 DEMO，LIVE 目前不下单）
LEV_MIN = 1
LEV_MAX = 20


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
    """
    main.py 只调用这个函数获取订单列表。
    """
    # 实盘继续锁死，确保现在所有改动只在 DEMO 生效
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

            direction, trend_strength = detect_trend_direction(trend)
            side, move_ratio = detect_entry_signal(entry, direction)

            if side is None or direction == "flat":
                continue

            reason = (
                f"现货混合策略: trend={direction}, "
                f"trend_strength={trend_strength*100:.2f}%, "
                f"move={move_ratio*100:.2f}%, signal={side.value}"
            )

            order = OrderRequest(
                env=env,
                market=MarketType.SPOT,
                symbol=symbol,
                side=side,
                amount=0.001,  # 名义金额由 main.py 控制
                price=None,
                leverage=None,
                position_side=None,
                reason=reason,
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

            direction, trend_strength = detect_trend_direction(trend)
            side, move_ratio = detect_entry_signal(entry, direction)

            if side is None or direction == "flat":
                continue

            conf_level, lev = decide_leverage(trend_strength, move_ratio)

            reason = (
                f"合约混合策略: trend={direction}, "
                f"trend_strength={trend_strength*100:.2f}%, "
                f"move={move_ratio*100:.2f}%, "
                f"conf={conf_level}, lev={lev}x, signal={side.value}"
            )

            order = OrderRequest(
                env=env,
                market=MarketType.FUTURES,
                symbol=symbol,
                side=side,
                amount=1,           # 张数占位，名义金额由 main.py 压到 ~10–20U
                price=None,
                leverage=lev,
                position_side=None, # 单向持仓模式
                reason=reason,
            )
            results.append(order)

        except Exception as e:
            print(f"[FUTURES] {symbol} 策略错误: {e}")

    return results


# =============================
# 趋势判断：MA5 / MA20 + 强度
# =============================

def detect_trend_direction(trend_kline: List[List[Any]]) -> Tuple[str, float]:
    """
    返回 (direction, strength)，direction in {"up","down","flat"}，
    strength 是 MA5/MA20 差值百分比。
    """
    closes = [c[4] for c in trend_kline]
    if len(closes) < 20:
        return "flat", 0.0

    ma_fast = sum(closes[-5:]) / 5
    ma_slow = sum(closes[-20:]) / 20

    if ma_slow == 0:
        return "flat", 0.0

    diff_ratio = abs(ma_fast - ma_slow) / ma_slow

    if diff_ratio < MIN_TREND_STRENGTH:
        return "flat", diff_ratio

    if ma_fast > ma_slow:
        return "up", diff_ratio
    elif ma_fast < ma_slow:
        return "down", diff_ratio
    else:
        return "flat", diff_ratio


# =============================
# 入场信号：1h 动量 + 最小波动
# =============================

def detect_entry_signal(entry_kline: List[List[Any]], trend: str) -> Tuple[Optional[Side], float]:
    """
    返回 (side, move_ratio)，side 可能为 None。
    """
    if trend not in ("up", "down"):
        return None, 0.0

    closes = [c[4] for c in entry_kline]
    if len(closes) < 3:
        return None, 0.0

    last = closes[-1]
    prev = closes[-2]

    if prev == 0:
        return None, 0.0

    move_ratio = abs(last - prev) / prev
    if move_ratio < MIN_ENTRY_MOVE:
        # 波动太小，不交易
        return None, move_ratio

    if trend == "up" and last > prev:
        return Side.BUY, move_ratio
    if trend == "down" and last < prev:
        return Side.SELL, move_ratio

    return None, move_ratio


# =============================
# 根据信号强弱决定杠杆（1x ~ 20x）
# =============================

def decide_leverage(trend_strength: float, move_ratio: float) -> Tuple[str, int]:
    """
    根据趋势强度和入场动量决定杠杆：
        - weak   -> 2x
        - normal -> 4x
        - strong -> 8x
        - ultra  -> 15x
    """
    ts = trend_strength / (MIN_TREND_STRENGTH + 1e-9)
    mv = move_ratio / (MIN_ENTRY_MOVE + 1e-9)

    score = (ts + mv) / 2  # 简单平均

    if score < 2:
        level = "weak"
        lev = 2
    elif score < 4:
        level = "normal"
        lev = 4
    elif score < 8:
        level = "strong"
        lev = 8
    else:
        level = "ultra"
        lev = 15

    lev = max(LEV_MIN, min(lev, LEV_MAX))
    return level, lev
