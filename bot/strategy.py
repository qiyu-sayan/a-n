# bot/strategy.py
from __future__ import annotations

import os
from typing import List

from bot.trader import (
    Trader,
    OrderRequest,
    Env,
    MarketType,
    Side,
    PositionSide,
)


# =============================
# 策略配置
# =============================

# 需要交易的合约品种（OKX 永续）
# 如果以后想增删，只改这里即可
FUTURE_SYMBOLS = [
    "ETH-USDT-SWAP",
    "BTC-USDT-SWAP",
    "ZEC-USDT-SWAP",
    "ASTR-USDT-SWAP",  # 你说的 aster，这边按 OKX 上的 ASTR-USDT-SWAP 写
]

# 使用的 K 线周期，默认 4 小时
TIMEFRAME = os.getenv("STRAT_TIMEFRAME", "4h")

# 单笔目标名义金额（USDT），默认跟 main.py 里的 ORDER_USDT 一致
TARGET_NOTIONAL_USDT = float(os.getenv("ORDER_USDT", "10"))

# 默认合约杠杆
DEFAULT_FUT_LEVERAGE = int(os.getenv("FUT_LEVERAGE", "5"))

# EMA 周期
EMA_FAST = 20
EMA_SLOW = 50

# 放量因子：最新一根 K 线成交量 > 最近 N 根平均 * VOL_FACTOR 才算“放量”
VOL_LOOKBACK = 20
VOL_FACTOR = 1.2


# =============================
# 工具函数
# =============================

def _ema(values: List[float], period: int) -> List[float]:
    """简单 EMA 实现，返回同长度列表，前 period-1 个用第一个有效值填充。"""
    if not values or period <= 1:
        return values[:]

    alpha = 2 / (period + 1)
    ema_vals: List[float] = [values[0]]
    for v in values[1:]:
        ema_vals.append(alpha * v + (1 - alpha) * ema_vals[-1])

    # 为了方便，只要长度一致即可
    return ema_vals


def _calc_trend_signals(closes: List[float], volumes: List[float]) -> str:
    """
    根据 EMA20 / EMA50 + 放量判断多空信号：

    返回:
        "long"  -> 做多信号
        "short" -> 做空信号
        "none"  -> 无信号
    """
    if len(closes) < max(EMA_FAST, EMA_SLOW) + 5:
        return "none"

    ema_fast = _ema(closes, EMA_FAST)
    ema_slow = _ema(closes, EMA_SLOW)

    last_close = closes[-1]
    last_fast = ema_fast[-1]
    last_slow = ema_slow[-1]

    # 最近 N 根平均成交量，用于判断“放量”
    if len(volumes) >= VOL_LOOKBACK + 1:
        avg_vol = sum(volumes[-(VOL_LOOKBACK + 1):-1]) / VOL_LOOKBACK
    else:
        avg_vol = sum(volumes[:-1]) / max(len(volumes) - 1, 1)
    last_vol = volumes[-1]

    is_up_trend = last_fast > last_slow
    is_down_trend = last_fast < last_slow
    is_bull_break = last_close > last_fast
    is_bear_break = last_close < last_fast
    is_volume_spike = last_vol > avg_vol * VOL_FACTOR

    # 简单规则：
    # 1）当前 fast > slow 且收盘在 fast 之上 + 放量 -> 做多信号
    if is_up_trend and is_bull_break and is_volume_spike:
        return "long"

    # 2）fast < slow 且收盘在 fast 之下 + 放量 -> 做空信号
    if is_down_trend and is_bear_break and is_volume_spike:
        return "short"

    return "none"


def _build_futures_order(
    trader: Trader,
    symbol: str,
    signal: str,
    last_price: float,
) -> OrderRequest:
    """
    由信号构造一个 OrderRequest（只开仓，不平仓，reduce_only=False）。
    """
    # 目标名义金额 -> 数量
    notional = TARGET_NOTIONAL_USDT
    if last_price <= 0:
        # 极端情况，防止除零，给一个非常小的默认数量
        amount = 0.001
    else:
        amount = notional / last_price

    if signal == "long":
        side = Side.BUY
        pos_side = PositionSide.LONG
        reason = f"{TIMEFRAME} EMA 多头趋势 + 放量突破"
    else:  # "short"
        side = Side.SELL
        pos_side = PositionSide.SHORT
        reason = f"{TIMEFRAME} EMA 空头趋势 + 放量跌破"

    return OrderRequest(
        env=trader.env,
        market=MarketType.FUTURES,
        symbol=symbol,
        side=side,
        amount=amount,
        leverage=DEFAULT_FUT_LEVERAGE,
        position_side=pos_side,
        reduce_only=False,
        reason=reason,
    )


# =============================
# 主策略入口
# =============================

def generate_orders(trader: Trader) -> List[OrderRequest]:
    """
    策略主入口（main.py 会直接调用这个函数）。

    当前逻辑：
    - 只做 OKX 合约（SWAP）
    - 4 小时 K 线
    - 对每个设定好的品种：
        * 计算 EMA20 / EMA50
        * 判断是否处于趋势行情（多 / 空）
        * 判断是否有“放量突破 / 跌破”
        * 有信号 -> 生成一个对应方向的开仓订单
    - 返回的订单会交给 main.py 做统一风控、下单、记录、推送。
    """
    orders: List[OrderRequest] = []

    fut = trader.futures

    for symbol in FUTURE_SYMBOLS:
        try:
            # 获取 K 线数据： [timestamp, open, high, low, close, volume]
            ohlcv = fut.fetch_ohlcv(symbol, timeframe=TIMEFRAME, limit=120)
            if not ohlcv or len(ohlcv) < 60:
                print(f"[strategy] {symbol} K线数据不足，跳过。")
                continue

            closes = [c[4] for c in ohlcv]
            vols = [c[5] for c in ohlcv]
            last_price = closes[-1]

            signal = _calc_trend_signals(closes, vols)

            if signal == "none":
                print(f"[strategy] {symbol} 当前无信号。")
                continue

            order = _build_futures_order(trader, symbol, signal, last_price)
            orders.append(order)

            print(
                f"[strategy] 生成 {symbol} 信号: {signal}, "
                f"last_price={last_price:.4f}, amount~{order.amount:.6f}"
            )

        except Exception as e:
            print(f"[strategy] 处理 {symbol} 时出错: {e}")
            continue

    return orders
