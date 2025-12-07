# bot/strategy.py
"""
混合策略系统（现货 + 合约 + 多周期）

当前版本说明：
----------------
- 只在 Env.TEST（OKX 模拟盘）下单
- Env.LIVE（实盘）直接返回空列表，不会下任何单
- 同时开启现货 & 合约策略
- 使用 4h K 线判断大趋势（MA5 / MA20）
- 使用 1h K 线做简单动量入场：
    - 趋势向上且最新 1h 收盘 > 前一根收盘 → BUY
    - 趋势向下且最新 1h 收盘 < 前一根收盘 → SELL
- 每次运行可能同时给出：
    - 现货 BTC/USDT 1 笔
    - 合约 BTC/USDT:USDT 1 笔（3x 杠杆，单向模式）

注意：
- run-bot 目前每 30min 跑一次，而入场周期是 1h，
  所以同一根 1h K 线内可能触发多次同方向信号（会重复加仓）。
  这在模拟盘可以接受，后面我们可以再加“去重/控频”。
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

# 现货 / 合约都开
ENABLE_SPOT = True
ENABLE_FUTURES = True

# 多周期参数
TREND_TF = "4h"   # 趋势周期
ENTRY_TF = "1h"   # 入场周期

# 交易品种（先只跑 BTC，后面你可以自己加 ETH 等）
SPOT_SYMBOLS = ["BTC/USDT"]
FUTURES_SYMBOLS = ["BTC/USDT:USDT"]


# =============================
# ccxt 工具
# =============================

def get_exchange_spot() -> ccxt.Exchange:
    ex = ccxt.okx()
    ex.options["defaultType"] = "spot"
    return ex


def get_exchange_futures() -> ccxt.Exchange:
    ex = ccxt.okx()
    ex.options["defaultType"] = "swap"  # OKX 永续
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
    main.py 每次只调用这个函数获取订单列表。
    """

    # 关键保护：实盘目前禁用所有策略
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
                amount=0.001,       # 非常小的 0.001 BTC，模拟盘够用
                price=None,         # 市价单
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
                amount=1,           # 1 张，配合 3x 杠杆，风险很小（模拟盘用）
                price=None,         # 市价单
                leverage=3,
                position_side=None, # 单向持仓模式
                reason=f"合约混合策略: trend={direction}, signal={side.value}",
            )
            results.append(order)

        except Exception as e:
            print(f"[FUTURES] {symbol} 策略错误: {e}")

    return results


# =============================
# 趋势判断：MA5 / MA20
# =============================

def detect_trend_direction(trend_kline: List[List[Any]]) -> str:
    """
    简单趋势判断：
        MA5 > MA20 → up（看多）
        MA5 < MA20 → down（看空）
        否则 → flat（不交易）
    """
    closes = [c[4] for c in trend_kline]
    if len(closes) < 20:
        return "flat"

    ma_fast = sum(closes[-5:]) / 5
    ma_slow = sum(closes[-20:]) / 20

    if ma_fast > ma_slow:
        return "up"
    elif ma_fast < ma_slow:
        return "down"
    else:
        return "flat"


# =============================
# 入场信号：1h 简单动量
# =============================

def detect_entry_signal(entry_kline: List[List[Any]], trend: str) -> Optional[Side]:
    """
    入场条件（非常简化版，只是为了让 DEMO 跑起来）：

        - trend = "up"，且最新一根 1h 收盘 > 前一根收盘 → BUY
        - trend = "down"，且最新一根 1h 收盘 < 前一根收盘 → SELL
        - 其余情况 → 不交易（返回 None）
    """
    if trend not in ("up", "down"):
        return None

    closes = [c[4] for c in entry_kline]
    if len(closes) < 3:
        return None

    last = closes[-1]
    prev = closes[-2]

    if trend == "up" and last > prev:
        return Side.BUY
    if trend == "down" and last < prev:
        return Side.SELL

    return None
