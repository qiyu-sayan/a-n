# bot/strategy.py
from __future__ import annotations

import os
from typing import List, Tuple

from bot.trader import (
    Trader,
    OrderRequest,
    MarketType,
    Side,
    PositionSide,
)

# =============================
# 策略配置
# =============================

# 交易品种：ETH / BTC 合约
FUTURE_SYMBOLS = [
    "ETH-USDT-SWAP",
    "BTC-USDT-SWAP",
]

# 趋势周期：4h
TF_TREND = os.getenv("STRAT_TF_TREND", "4h")
# 入场周期：1h
TF_ENTRY = os.getenv("STRAT_TF_ENTRY", "1h")

# 单笔目标名义金额（USDT），和 main.py / Secrets 中 ORDER_USDT 保持一致
TARGET_NOTIONAL_USDT = float(os.getenv("ORDER_USDT", "10"))

# 杠杆档位：信号越强，用的杠杆越高，但不超过 MAX
LEV_MIN = int(os.getenv("LEV_MIN", "3"))   # 弱信号
LEV_MID = int(os.getenv("LEV_MID", "5"))   # 中等信号
LEV_MAX = int(os.getenv("LEV_MAX", "8"))   # 强信号（建议别太大）

# EMA 周期
EMA_FAST_4H = 20
EMA_SLOW_4H = 50
EMA_ENTRY_1H = 20


# =============================
# 工具函数
# =============================

def _ema(values: List[float], period: int) -> List[float]:
    """简单 EMA 实现。"""
    if not values or period <= 1:
        return values[:]

    alpha = 2 / (period + 1)
    ema_vals: List[float] = [values[0]]
    for v in values[1:]:
        ema_vals.append(alpha * v + (1 - alpha) * ema_vals[-1])
    return ema_vals


# ---------- 4h 趋势评估 ----------

def _assess_trend_4h(closes: List[float]) -> Tuple[str, int]:
    """
    用 4h EMA20 / EMA50 判断趋势方向 + 粗略强度评分。

    返回:
        trend: "up" / "down" / "none"
        score: 0 / 1 / 2  （越大趋势越强）
    """
    min_len = max(EMA_FAST_4H, EMA_SLOW_4H) + 5
    if len(closes) < min_len:
        return "none", 0

    ema_fast = _ema(closes, EMA_FAST_4H)
    ema_slow = _ema(closes, EMA_SLOW_4H)

    # 用上一根收盘 K（已收线）
    idx = -2
    last_fast = ema_fast[idx]
    last_slow = ema_slow[idx]

    if last_fast > last_slow:
        trend = "up"
    elif last_fast < last_slow:
        trend = "down"
    else:
        return "none", 0

    # 趋势强度：快慢线乖离百分比
    base = abs(last_fast - last_slow)
    if last_slow != 0:
        diff_pct = base / abs(last_slow) * 100
    else:
        diff_pct = 0.0

    # 评分：0 / 1 / 2
    if diff_pct < 0.3:
        score = 0          # 几乎缠绕，趋势弱
    elif diff_pct < 1.0:
        score = 1          # 普通趋势
    else:
        score = 2          # 趋势比较明显

    return trend, score


# ---------- 1h 入场信号 ----------

def _entry_signal_1h(
    closes: List[float],
    trend: str,
) -> Tuple[str, int]:
    """
    用 1h EMA20 决定是否入场：

    - 如果 4h 多头趋势：
        * 上一根在 EMA 下方，这一根收盘站上 EMA -> 多头信号
    - 如果 4h 空头趋势：
        * 上一根在 EMA 上方，这一根收盘跌破 EMA -> 空头信号

    返回:
        direction: "long" / "short" / "none"
        score: 0 / 1  （有信号 = 至少 1 分）
    """
    min_len = EMA_ENTRY_1H + 3
    if len(closes) < min_len or trend == "none":
        return "none", 0

    ema_entry = _ema(closes, EMA_ENTRY_1H)

    # 仍然用上一根已收线
    idx = -2
    prev_idx = -3

    prev_close = closes[prev_idx]
    prev_ema = ema_entry[prev_idx]

    last_close = closes[idx]
    last_ema = ema_entry[idx]

    if trend == "up":
        # 从下往上穿 → 做多
        if prev_close <= prev_ema and last_close > last_ema:
            return "long", 1
    elif trend == "down":
        # 从上往下穿 → 做空
        if prev_close >= prev_ema and last_close < last_ema:
            return "short", 1

    return "none", 0


# ---------- 信号强度 -> 杠杆 ----------

def _choose_leverage(trend_score: int, entry_score: int) -> int:
    """
    简单评分逻辑：

        总分 = 趋势强度 (0~2) + 入场确认 (0/1)

        总分 <= 1  -> LEV_MIN   （3x）
        总分 == 2  -> LEV_MID   （5x）
        总分 >= 3  -> LEV_MAX   （8x）

    你以后如果想更激进/更保守，可以调 env 里的 LEV_*。
    """
    total = trend_score + entry_score

    if total <= 1:
        return LEV_MIN
    elif total == 2:
        return LEV_MID
    else:
        return LEV_MAX


# ---------- 构造订单 ----------

def _build_futures_order(
    trader: Trader,
    symbol: str,
    direction: str,
    ref_price: float,
    lev: int,
    trend_desc: str,
    entry_desc: str,
) -> OrderRequest:
    """根据方向 + 杠杆 + 参考价格构造 OrderRequest。"""
    if ref_price <= 0:
        amount = 0.001
    else:
        amount = TARGET_NOTIONAL_USDT / ref_price

    if direction == "long":
        side = Side.BUY
        pos_side = PositionSide.LONG
    else:
        side = Side.SELL
        pos_side = PositionSide.SHORT

    reason = f"4h趋势: {trend_desc}; 1h入场: {entry_desc}; 动态杠杆: {lev}x"

    return OrderRequest(
        env=trader.env,
        market=MarketType.FUTURES,
        symbol=symbol,
        side=side,
        amount=amount,
        leverage=lev,
        position_side=pos_side,
        reduce_only=False,
        reason=reason,
    )


# =============================
# 主策略入口
# =============================

def generate_orders(trader: Trader) -> List[OrderRequest]:
    """
    策略主入口（被 main.py 调用）：

    - 先用 4h K 线评估趋势方向 + 强度（决定多 / 空 / 不交易 + 部分分数）
    - 再用 1h K 线判断是否出现「顺势的 EMA20 穿越信号」（决定是否入场 + 额外分数）
    - 根据 总分 -> 选择杠杆档位（弱 / 中 / 强）
    - 返回的订单统一交给 main.py 做风控、下单、日志、推送。
    """
    orders: List[OrderRequest] = []

    fut = trader.futures

    for symbol in FUTURE_SYMBOLS:
        try:
            # 4h 趋势数据
            ohlcv_4h = fut.fetch_ohlcv(symbol, timeframe=TF_TREND, limit=120)
            if not ohlcv_4h or len(ohlcv_4h) < 60:
                print(f"[strategy] {symbol} 4h K线数据不足，跳过。")
                continue
            closes_4h = [c[4] for c in ohlcv_4h]

            trend, trend_score = _assess_trend_4h(closes_4h)
            if trend == "none":
                print(f"[strategy] {symbol} 4h 无明确趋势，跳过。")
                continue

            trend_desc = f"{TF_TREND} EMA20/50 { '多头' if trend == 'up' else '空头' } (score={trend_score})"

            # 1h 入场数据
            ohlcv_1h = fut.fetch_ohlcv(symbol, timeframe=TF_ENTRY, limit=200)
            if not ohlcv_1h or len(ohlcv_1h) < 80:
                print(f"[strategy] {symbol} 1h K线数据不足，跳过。")
                continue
            closes_1h = [c[4] for c in ohlcv_1h]

            direction, entry_score = _entry_signal_1h(closes_1h, trend)
            if direction == "none":
                print(f"[strategy] {symbol} 有趋势但暂无线号。")
                continue

            entry_desc = f"{TF_ENTRY} EMA{EMA_ENTRY_1H} 穿越确认方向={direction} (score={entry_score})"

            # 选择杠杆
            lev = _choose_leverage(trend_score, entry_score)

            # 参考价格：用 1h 上一根收盘价
            ref_price = closes_1h[-2]

            order = _build_futures_order(
                trader=trader,
                symbol=symbol,
                direction=direction,
                ref_price=ref_price,
                lev=lev,
                trend_desc=trend_desc,
                entry_desc=entry_desc,
            )
            orders.append(order)

            print(
                f"[strategy] 生成 {symbol} 信号: dir={direction}, lev={lev}x, "
                f"ref_price={ref_price:.4f}, amount~{order.amount:.6f}, "
                f"trend_score={trend_score}, entry_score={entry_score}"
            )

        except Exception as e:
            print(f"[strategy] 处理 {symbol} 时出错: {e}")
            continue

    return orders
