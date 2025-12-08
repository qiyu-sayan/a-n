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

# 每个合约的最小下单数量（按 OKX 要求；可以以后再扩展/修改）
MIN_QTY = {
    "ETH-USDT-SWAP": 0.01,
    "BTC-USDT-SWAP": 0.001,
}

# 多周期：
TF_TREND = os.getenv("STRAT_TF_TREND", "4h")   # 趋势周期
TF_ENTRY_1H = os.getenv("STRAT_TF_ENTRY_1H", "1h")
TF_ENTRY_30M = os.getenv("STRAT_TF_ENTRY_30M", "30m")
TF_ENTRY_15M = os.getenv("STRAT_TF_ENTRY_15M", "15m")

# 策略模式：conservative / neutral / aggressive
STRAT_MODE = os.getenv("STRAT_MODE", "neutral").lower()
if STRAT_MODE not in {"conservative", "neutral", "aggressive"}:
    STRAT_MODE = "neutral"

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
EMA_ENTRY_30M = 20
EMA_ENTRY_15M = 20


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

def _assess_trend_4h(closes: List[float]) -> Tuple[str, int, str]:
    """
    用 4h EMA20 / EMA50 判断趋势方向 + 粗略强度评分。

    返回:
        trend: "up" / "down" / "none"
        score: 0 / 1 / 2  （越大趋势越强）
        desc: 文本描述
    """
    min_len = max(EMA_FAST_4H, EMA_SLOW_4H) + 5
    if len(closes) < min_len:
        return "none", 0, "4h K线数据不足"

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
        return "none", 0, "4h EMA20 与 EMA50 几乎重合，无趋势"

    # 趋势强度：快慢线乖离百分比
    if last_slow != 0:
        diff_pct = abs(last_fast - last_slow) / abs(last_slow) * 100
    else:
        diff_pct = 0.0

    # 评分：0 / 1 / 2（略微放宽一点）
    if diff_pct < 0.15:
        score = 0          # 几乎缠绕，趋势很弱
    elif diff_pct < 0.6:
        score = 1          # 普通趋势
    else:
        score = 2          # 趋势比较明显

    desc = f"{TF_TREND} EMA20/50 趋势：{'多头' if trend == 'up' else '空头'}，乖离约 {diff_pct:.2f}%，score={score}"
    return trend, score, desc


# ---------- 单一周期入场评分 ----------

def _entry_score_single_tf(
    closes: List[float],
    trend: str,
    period: int,
    tf_name: str,
) -> Tuple[int, str]:
    """
    给单一周期打入场分：

    - 只做顺势方向（4h 多头 -> 只考虑多；4h 空头 -> 只考虑空）
    - 评分规则：
        * 从反向一侧穿到趋势一侧：2 分（强信号）
        * 已经在趋势一侧并保持：1 分（普通信号）
        * 其余：0 分（反向或无信号）

    返回:
        score: 0 / 1 / 2
        desc: 文本描述
    """
    min_len = period + 3
    if len(closes) < min_len or trend == "none":
        return 0, f"{tf_name} K线数据不足或无趋势"

    ema_vals = _ema(closes, period)

    idx = -2
    prev_idx = -3

    prev_close = closes[prev_idx]
    last_close = closes[idx]
    prev_ema = ema_vals[prev_idx]
    last_ema = ema_vals[idx]

    side_text = "多" if trend == "up" else "空"

    # 趋势方向：多头 -> 希望价格在 EMA 上方；空头 -> 希望价格在 EMA 下方
    if trend == "up":
        # 从下向上穿越 -> 2 分
        if prev_close <= prev_ema and last_close > last_ema:
            return 2, f"{tf_name} 收盘价向上穿越 EMA{period}，顺势{side_text}强信号(+2)"
        # 一直站在 EMA 上方 -> 1 分
        elif last_close > last_ema:
            return 1, f"{tf_name} 收盘价位于 EMA{period} 上方，顺势{side_text}普通信号(+1)"
        else:
            return 0, f"{tf_name} 收盘价在 EMA{period} 下方，逆势/无信号(+0)"

    else:  # trend == "down"
        # 从上向下穿越 -> 2 分
        if prev_close >= prev_ema and last_close < last_ema:
            return 2, f"{tf_name} 收盘价向下穿越 EMA{period}，顺势{side_text}强信号(+2)"
        # 一直在 EMA 下方 -> 1 分
        elif last_close < last_ema:
            return 1, f"{tf_name} 收盘价位于 EMA{period} 下方，顺势{side_text}普通信号(+1)"
        else:
            return 0, f"{tf_name} 收盘价在 EMA{period} 上方，逆势/无信号(+0)"


# ---------- 多周期合成入场信号 ----------

def _entry_signal_multi_tf(
    closes_1h: List[float],
    closes_30m: List[float],
    closes_15m: List[float],
    trend: str,
) -> Tuple[str, int, str]:
    """
    综合 1h + 30m + 15m 得到入场方向和得分。

    direction:
        "long" / "short" / "none"
    score:
        0~6
    desc:
        用于 reason 的文本描述
    """
    if trend == "none":
        return "none", 0, "无 4h 趋势，不考虑入场"

    # 单周期评分
    score_1h, desc_1h = _entry_score_single_tf(
        closes_1h, trend, EMA_ENTRY_1H, TF_ENTRY_1H
    )
    score_30m, desc_30m = _entry_score_single_tf(
        closes_30m, trend, EMA_ENTRY_30M, TF_ENTRY_30M
    )
    score_15m, desc_15m = _entry_score_single_tf(
        closes_15m, trend, EMA_ENTRY_15M, TF_ENTRY_15M
    )

    total_score = score_1h + score_30m + score_15m

    # 不同模式的入场门槛
    if STRAT_MODE == "conservative":
        threshold = 3
    elif STRAT_MODE == "aggressive":
        threshold = 1
    else:  # neutral
        threshold = 2

    if total_score < threshold:
        desc = (
            f"{TF_ENTRY_1H}/{TF_ENTRY_30M}/{TF_ENTRY_15M} 顺势评分总分={total_score}，"
            f"未达到模式({STRAT_MODE})入场阈值={threshold}\n"
            f"- {desc_1h}\n- {desc_30m}\n- {desc_15m}"
        )
        return "none", total_score, desc

    direction = "long" if trend == "up" else "short"
    desc = (
        f"多周期顺势评分总分={total_score} (mode={STRAT_MODE}, 阈值={threshold})，"
        f"方向={direction}\n"
        f"- {desc_1h}\n- {desc_30m}\n- {desc_15m}"
    )
    return direction, total_score, desc


# ---------- 信号强度 -> 杠杆 ----------

def _choose_leverage(trend_score: int, entry_score: int) -> int:
    """
    总分 = 趋势强度 (0~2) + 入场强度 (0~6)。

    简化映射：
        total <= 2   -> LEV_MIN
        3 <= total <= 4 -> LEV_MID
        total >= 5   -> LEV_MAX
    """
    total = trend_score + entry_score

    if total <= 2:
        return LEV_MIN
    elif total <= 4:
        return LEV_MID
    else:
        return LEV_MAX


# ---------- 构造订单（带最小下单单位） ----------

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

    # 这里加入最小下单单位自动补足逻辑
    min_qty = MIN_QTY.get(symbol, 0.0)
    if amount < min_qty:
        print(
            f"[strategy] {symbol} 计算得到的数量 {amount:.6f} 小于最小下单单位 {min_qty}, "
            f"自动提升到 {min_qty}。"
        )
        amount = min_qty

    if direction == "long":
        side = Side.BUY
        pos_side = PositionSide.LONG
    else:
        side = Side.SELL
        pos_side = PositionSide.SHORT

    reason = (
        f"{trend_desc}\n"
        f"{entry_desc}\n"
        f"动态杠杆: {lev}x, 参考价: {ref_price:.4f}, 目标名义金额≈{TARGET_NOTIONAL_USDT} USDT "
        f"(实际数量已不低于最小单位 {min_qty})"
    )

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

    - 4h K 线：评估趋势方向 + 强度（决定多 / 空 / 不交易 + 部分分数）
    - 1h + 30m + 15m：多周期顺势评估，得到入场方向 + 强度分数
    - 根据 总分 -> 选择杠杆档位（LEV_MIN / MID / MAX）
    - 构造订单时保证数量 ≥ OKX 最小下单单位
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

            trend, trend_score, trend_desc = _assess_trend_4h(closes_4h)
            if trend == "none":
                print(f"[strategy] {symbol} {trend_desc}，跳过。")
                continue

            # 1h / 30m / 15m 入场数据
            ohlcv_1h = fut.fetch_ohlcv(symbol, timeframe=TF_ENTRY_1H, limit=200)
            ohlcv_30m = fut.fetch_ohlcv(symbol, timeframe=TF_ENTRY_30M, limit=200)
            ohlcv_15m = fut.fetch_ohlcv(symbol, timeframe=TF_ENTRY_15M, limit=200)

            if not ohlcv_1h or len(ohlcv_1h) < 80:
                print(f"[strategy] {symbol} 1h K线数据不足，跳过。")
                continue

            closes_1h = [c[4] for c in ohlcv_1h]
            closes_30m = [c[4] for c in ohlcv_30m] if ohlcv_30m else []
            closes_15m = [c[4] for c in ohlcv_15m] if ohlcv_15m else []

            direction, entry_score, entry_desc = _entry_signal_multi_tf(
                closes_1h, closes_30m, closes_15m, trend
            )

            if direction == "none":
                print(
                    f"[strategy] {symbol} 有 4h 趋势但多周期顺势得分过低："
                    f"trend_score={trend_score}, entry_score={entry_score}。"
                )
                continue

            # 杠杆
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
                f"trend_score={trend_score}, entry_score={entry_score}, mode={STRAT_MODE}"
            )

        except Exception as e:
            print(f"[strategy] 处理 {symbol} 时出错: {e}")
            continue

    return orders
