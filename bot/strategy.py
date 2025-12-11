"""
strategy.py

职责：
- 根据 K 线数据 + 配置，生成交易信号
- 分两层：
  1）基础信号层（双均线方向） -> raw_signal ∈ {-1, 0, 1}
  2）过滤层（趋势过滤 + RSI + 暴跌过滤） -> final_signal ∈ {-1, 0, 1}

说明：
- 本文件不直接下单，只负责“要不要做多/做空/观望”的决策。
"""

from typing import List, Dict, Any, Tuple, Optional


# ---------- 工具函数：从 K 线提取收盘价 ----------

def extract_closes(klines: List[List[Any]]) -> List[float]:
    """
    默认按 OKX / Binance 标准 K 线格式：
    [ts, open, high, low, close, volume, ...]
    如果你的格式不同，可以在这里改。
    """
    closes: List[float] = []
    for k in klines:
        try:
            closes.append(float(k[4]))
        except (IndexError, TypeError, ValueError):
            continue
    return closes


# ---------- 技术指标 ----------

def ema(series: List[float], period: int) -> List[float]:
    if not series or period <= 0:
        return []

    k = 2 / (period + 1)
    ema_vals: List[float] = []
    ema_prev = series[0]
    ema_vals.append(ema_prev)

    for price in series[1:]:
        ema_prev = price * k + ema_prev * (1 - k)
        ema_vals.append(ema_prev)

    return ema_vals


def rsi(series: List[float], period: int) -> List[Optional[float]]:
    """
    标准 Wilder RSI 实现。
    返回与 series 等长的 RSI 列表，前期用 None 填充。
    """
    n = len(series)
    if n == 0 or period <= 0 or n < period + 1:
        return []

    deltas = [series[i] - series[i - 1] for i in range(1, n)]
    gains = [max(d, 0.0) for d in deltas]
    losses = [max(-d, 0.0) for d in deltas]

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    rsi_vals: List[Optional[float]] = [None] * (period + 1)
    if avg_loss == 0:
        rsi_vals[-1] = 100.0
    else:
        rs = avg_gain / avg_loss
        rsi_vals[-1] = 100 - (100 / (1 + rs))

    for i in range(period, len(deltas)):
        gain = gains[i]
        loss = losses[i]

        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period

        if avg_loss == 0:
            rsi_vals.append(100.0)
        else:
            rs = avg_gain / avg_loss
            rsi_vals.append(100 - (100 / (1 + rs)))

    while len(rsi_vals) < n:
        rsi_vals.insert(0, None)

    return rsi_vals[:n]


# ---------- ① 基础信号层：双均线“方向信号” ----------

def base_signal_from_ma(
    closes: List[float],
    fast: int,
    slow: int,
    sig: int,  # 预留，用于未来扩展
) -> Tuple[int, Dict[str, float]]:
    """
    返回原始信号 raw_signal ∈ {-1, 0, 1}

    逻辑：
    - fast_ema > slow_ema 且差值足够 → 1（偏多）
    - fast_ema < slow_ema 且差值足够 → -1（偏空）
    - 两条线非常接近 → 0（认为是震荡，不发信号）

    注意：
    - 这样每根 K 都会给出一个方向，但我们在 main.py 里只在
      “当前持仓方向 != 信号方向” 时才开新仓，所以不会疯狂加仓。
    """
    info: Dict[str, float] = {}

    if len(closes) < slow + 1:
        return 0, info

    fast_ema = ema(closes, fast)
    slow_ema = ema(closes, slow)

    fast_last = fast_ema[-1]
    slow_last = slow_ema[-1]
    diff = fast_last - slow_last
    rel_diff = diff / slow_last if slow_last != 0 else 0.0

    info["fast_ema"] = fast_last
    info["slow_ema"] = slow_last
    info["ma_diff"] = diff
    info["ma_rel_diff"] = rel_diff

    # 阈值：当快慢 EMA 差距小于 0.03% 时认为是“几乎重合”，不做方向判断
    flat_threshold = 0.0003
    if abs(rel_diff) < flat_threshold:
        return 0, info

    if rel_diff > 0:
        return 1, info
    else:
        return -1, info


# ---------- ② 过滤层：趋势 / RSI / 暴跌 ----------

def detect_htf_trend(
    htf_closes: List[float],
    ma_period: int,
) -> str:
    if not htf_closes or len(htf_closes) < ma_period:
        return "unknown"

    ma_vals = ema(htf_closes, ma_period)
    last_close = htf_closes[-1]
    last_ma = ma_vals[-1]

    if last_close > last_ma:
        return "up"
    elif last_close < last_ma:
        return "down"
    else:
        return "flat"


def detect_crash(
    closes: List[float],
    lookback: int,
    threshold: float,
) -> bool:
    if len(closes) < lookback + 1 or lookback <= 0:
        return False

    last = closes[-1]
    prev = closes[-1 - lookback]

    pct_change = (last - prev) / prev
    return pct_change <= threshold


def apply_filters(
    raw_signal: int,
    closes: List[float],
    htf_closes: Optional[List[float]],
    cfg_filters: Dict[str, Any],
    debug_info: Dict[str, Any],
) -> int:
    final_signal = raw_signal

    # --- 1）高周期趋势过滤 ---
    htf_trend = "unknown"
    htf_period = int(cfg_filters.get("htf_ma_period", 50))
    if htf_closes:
        htf_trend = detect_htf_trend(htf_closes, htf_period)
    debug_info["htf_trend"] = htf_trend

    if final_signal == 1 and htf_trend == "down":
        debug_info["blocked_by_htf_trend"] = "long_blocked_in_downtrend"
        final_signal = 0
    elif final_signal == -1 and htf_trend == "up":
        debug_info["blocked_by_htf_trend"] = "short_blocked_in_uptrend"
        final_signal = 0

    # --- 2）RSI 过滤（默认阈值偏宽松） ---
    rsi_period = int(cfg_filters.get("rsi_period", 14))
    rsi_vals = rsi(closes, rsi_period)
    last_rsi = rsi_vals[-1] if rsi_vals else None
    debug_info["rsi"] = last_rsi

    long_overbought = float(cfg_filters.get("rsi_long_overbought", 80))  # 默认 80，比较难挡住多头
    short_oversold = float(cfg_filters.get("rsi_short_oversold", 20))    # 默认 20，比较难挡住空头

    if last_rsi is not None:
        if final_signal == 1 and last_rsi >= long_overbought:
            debug_info["blocked_by_rsi"] = "long_blocked_overbought"
            final_signal = 0
        elif final_signal == -1 and last_rsi <= short_oversold:
            debug_info["blocked_by_rsi"] = "short_blocked_oversold"
            final_signal = 0

    # --- 3）暴跌过滤（默认只有极端大跌才挡住做多） ---
    crash_lookback = int(cfg_filters.get("crash_lookback", 6))
    crash_threshold = float(cfg_filters.get("crash_threshold", -0.07))  # 最近 N 根累计跌幅 <= -7% 才算暴跌
    crashed = detect_crash(closes, crash_lookback, crash_threshold)
    debug_info["crashed"] = crashed

    if crashed and final_signal == 1:
        debug_info["blocked_by_crash"] = "long_blocked_after_crash"
        final_signal = 0

    return final_signal


# ---------- 对外主函数 ----------

def generate_signal(
    symbol: str,
    klines: List[List[Any]],
    cfg: Dict[str, Any],
    htf_klines: Optional[List[List[Any]]] = None,
    debug: bool = False,
) -> Tuple[int, Dict[str, Any]]:
    debug_info: Dict[str, Any] = {"symbol": symbol}

    closes = extract_closes(klines)
    if len(closes) < 10:
        debug_info["reason"] = "not_enough_data"
        return 0, debug_info

    logic = cfg.get("logic", {})
    filters_cfg = cfg.get("filters", {})

    fast = int(logic.get("fast", 12))
    slow = int(logic.get("slow", 26))
    sig = int(logic.get("sig", 9))

    # ① 基础信号层
    raw_signal, base_info = base_signal_from_ma(closes, fast, slow, sig)
    debug_info.update(base_info)
    debug_info["raw_signal"] = raw_signal
    debug_info["fast"] = fast
    debug_info["slow"] = slow
    debug_info["sig"] = sig

    if raw_signal == 0:
        # 没有明显方向（快慢 EMA 几乎重合），这里直接返回 0
        debug_info["reason"] = "ma_flat"
        if debug:
            print(f"[DEBUG][strategy] {symbol} -> raw=0, info={debug_info}")
        return 0, debug_info

    # ② 过滤层
    htf_closes = extract_closes(htf_klines) if htf_klines else None
    final_signal = apply_filters(
        raw_signal=raw_signal,
        closes=closes,
        htf_closes=htf_closes,
        cfg_filters=filters_cfg,
        debug_info=debug_info,
    )
    debug_info["final_signal"] = final_signal

    if debug:
        print(f"[DEBUG][strategy] {symbol} -> raw={raw_signal}, final={final_signal}, info={debug_info}")

    return final_signal, debug_info
