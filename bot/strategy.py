"""
strategy.py

职责：
- 根据 K 线数据 + 配置，生成交易信号
- 分两层：
  1）基础信号层（双均线方向） -> raw_signal ∈ {-1, 0, 1}
  2）过滤层：
     - 高周期趋势过滤
     - RSI 过滤
     - 暴跌过滤
     - 突破确认过滤（新加）
     - ATR 波动率过滤
     - EMA 斜率过滤

新增（B）：回踩二次入场（Pullback Re-entry）
- 高周期趋势：HTF EMA50/EMA200 + HTF close 在 EMA50 同侧
- 入场周期：回踩到 EMA20/EMA30 “附近” + 不破 EMA50 守卫 + 下一根确认K
- 推送解释：在 debug_info 里提供 signal_name/signal_reason/关键参数
"""

from typing import List, Dict, Any, Tuple, Optional


# ---------- 工具函数：从 K 线提取 OHLC ----------

def extract_ohlc(klines: List[List[Any]]) -> Tuple[List[float], List[float], List[float], List[float]]:
    """
    默认按 OKX / Binance 标准 K 线格式：
    [ts, open, high, low, close, volume, ...]
    """
    opens: List[float] = []
    highs: List[float] = []
    lows: List[float] = []
    closes: List[float] = []
    for k in klines:
        try:
            opens.append(float(k[1]))
            highs.append(float(k[2]))
            lows.append(float(k[3]))
            closes.append(float(k[4]))
        except (IndexError, TypeError, ValueError):
            continue
    return opens, highs, lows, closes


def extract_closes(klines: List[List[Any]]) -> List[float]:
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


def atr_from_closes(series: List[float], period: int) -> Optional[float]:
    n = len(series)
    if n < period + 1 or period <= 0:
        return None

    trs = [abs(series[i] - series[i - 1]) for i in range(1, n)]
    recent_trs = trs[-period:]
    if not recent_trs:
        return None
    return sum(recent_trs) / len(recent_trs)


def recent_high_low(series: List[float], lookback: int) -> Tuple[Optional[float], Optional[float]]:
    n = len(series)
    if n <= lookback:
        return None, None

    window = series[-1 - lookback:-1]
    return max(window), min(window)


def pct_diff(a: float, b: float) -> float:
    if b == 0:
        return 999.0
    return abs(a - b) / abs(b)


# ---------- ① 基础信号层：双均线“方向信号” ----------

def base_signal_from_ma(
    closes: List[float],
    fast: int,
    slow: int,
    sig: int,
) -> Tuple[int, Dict[str, float]]:
    info: Dict[str, float] = {}

    if len(closes) < slow + 1:
        return 0, info

    fast_ema_vals = ema(closes, fast)
    slow_ema_vals = ema(closes, slow)

    fast_last = fast_ema_vals[-1]
    slow_last = slow_ema_vals[-1]
    diff = fast_last - slow_last
    rel_diff = diff / slow_last if slow_last != 0 else 0.0

    info["fast_ema"] = fast_last
    info["slow_ema"] = slow_last
    info["ma_diff"] = diff
    info["ma_rel_diff"] = rel_diff

    flat_threshold = 0.0003
    if abs(rel_diff) < flat_threshold:
        return 0, info

    if rel_diff > 0:
        return 1, info
    else:
        return -1, info


# ---------- ② 过滤层：趋势 / RSI / 暴跌 / 突破 / 波动率 / 斜率 ----------

def detect_htf_trend(htf_closes: List[float], ma_period: int) -> str:
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


def detect_crash(closes: List[float], lookback: int, threshold: float) -> bool:
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

    # --- 2）RSI 过滤 ---
    rsi_period = int(cfg_filters.get("rsi_period", 14))
    rsi_vals = rsi(closes, rsi_period)
    last_rsi = rsi_vals[-1] if rsi_vals else None
    debug_info["rsi"] = last_rsi

    long_overbought = float(cfg_filters.get("rsi_long_overbought", 80))
    short_oversold = float(cfg_filters.get("rsi_short_oversold", 20))

    if last_rsi is not None:
        if final_signal == 1 and last_rsi >= long_overbought:
            debug_info["blocked_by_rsi"] = "long_blocked_overbought"
            final_signal = 0
        elif final_signal == -1 and last_rsi <= short_oversold:
            debug_info["blocked_by_rsi"] = "short_blocked_oversold"
            final_signal = 0

    # --- 3）暴跌过滤 ---
    crash_lookback = int(cfg_filters.get("crash_lookback", 6))
    crash_threshold = float(cfg_filters.get("crash_threshold", -0.07))
    crashed = detect_crash(closes, crash_lookback, crash_threshold)
    debug_info["crashed"] = crashed

    if crashed and final_signal == 1:
        debug_info["blocked_by_crash"] = "long_blocked_after_crash"
        final_signal = 0

    # --- 4）突破确认过滤（适用于突破类，不适用于B；B会在自己的逻辑里跳过） ---
    breakout_lookback = int(cfg_filters.get("breakout_lookback", 5))
    last_close = closes[-1]

    if final_signal != 0 and breakout_lookback > 0:
        recent_high, recent_low = recent_high_low(closes, breakout_lookback)
        debug_info["recent_high"] = recent_high
        debug_info["recent_low"] = recent_low

        if recent_high is not None and recent_low is not None:
            if final_signal == 1:
                if not (last_close > recent_high):
                    debug_info["blocked_by_breakout"] = f"no_long_breakout: close={last_close} <= high={recent_high}"
                    final_signal = 0
            elif final_signal == -1:
                if not (last_close < recent_low):
                    debug_info["blocked_by_breakout"] = f"no_short_breakout: close={last_close} >= low={recent_low}"
                    final_signal = 0

    # --- 5）ATR 波动率过滤 ---
    atr_period = int(cfg_filters.get("atr_period", 14))
    atr_min_pct = float(cfg_filters.get("atr_min_pct", 0.002))

    atr_val = atr_from_closes(closes, atr_period)
    atr_pct = None
    if atr_val is not None and closes[-1] != 0:
        atr_pct = atr_val / closes[-1]

    debug_info["atr"] = atr_val
    debug_info["atr_pct"] = atr_pct

    if atr_pct is not None and atr_pct < atr_min_pct:
        debug_info["blocked_by_atr"] = f"low_vol: atr_pct={atr_pct:.6f} < {atr_min_pct:.6f}"
        final_signal = 0

    # --- 6）EMA 斜率过滤 ---
    fast_len = int(debug_info.get("fast", cfg_filters.get("fast_len_for_slope", 12)))
    slope_lookback = int(cfg_filters.get("slope_lookback", 3))
    slope_min_abs = float(cfg_filters.get("slope_min_abs", 0.0005))

    fast_ema_vals = ema(closes, fast_len)
    fast_slope = None
    if len(fast_ema_vals) > slope_lookback:
        prev_val = fast_ema_vals[-1 - slope_lookback]
        last_val = fast_ema_vals[-1]
        if prev_val != 0:
            fast_slope = (last_val - prev_val) / prev_val

    debug_info["fast_slope"] = fast_slope

    if fast_slope is not None:
        if abs(fast_slope) < slope_min_abs:
            debug_info["blocked_by_slope"] = f"flat_ma: slope={fast_slope:.6f} < {slope_min_abs:.6f}"
            final_signal = 0
        else:
            if final_signal == 1 and fast_slope < 0:
                debug_info["blocked_by_slope"] = f"long_blocked_by_down_slope: slope={fast_slope:.6f}"
                final_signal = 0
            elif final_signal == -1 and fast_slope > 0:
                debug_info["blocked_by_slope"] = f"short_blocked_by_up_slope: slope={fast_slope:.6f}"
                final_signal = 0

    return final_signal


# ---------- B：回踩二次入场 ----------

def detect_trend_ma50_200(htf_closes: List[float], fast: int = 50, slow: int = 200) -> Tuple[str, Dict[str, Any]]:
    """
    返回：("bull"/"bear"/"none", tags)
    """
    info: Dict[str, Any] = {}
    if len(htf_closes) < slow + 2:
        return "none", {"trend": "insufficient_htf"}

    ema_fast = ema(htf_closes, fast)[-1]
    ema_slow = ema(htf_closes, slow)[-1]
    last = htf_closes[-1]

    info["htf_ema50"] = ema_fast
    info["htf_ema200"] = ema_slow
    info["htf_close"] = last

    if ema_fast > ema_slow and last > ema_fast:
        info["trend"] = "bull"
        return "bull", info
    if ema_fast < ema_slow and last < ema_fast:
        info["trend"] = "bear"
        return "bear", info

    info["trend"] = "none"
    return "none", info


def generate_signal_b_pullback(
    symbol: str,
    klines: List[List[Any]],
    htf_klines: List[List[Any]],
    cfg: Dict[str, Any],
    debug_info: Dict[str, Any],
) -> int:
    """
    B：回踩二次入场（只用必要过滤：趋势+回踩+确认，外加可选 RSI/ATR）
    """
    logic = cfg.get("logic", {})
    filters_cfg = cfg.get("filters", {})

    # --- 参数（可在 config/params.json 里调） ---
    b = logic.get("B", {}) if isinstance(logic.get("B", {}), dict) else {}
    pull_near_pct = float(b.get("pullback_near_pct", 0.003))    # 0.3%
    guard_break_pct = float(b.get("guard_break_pct", 0.0))      # 允许轻微穿越（默认0=不允许）
    require_structure = bool(b.get("require_structure", True))  # 要求“从上回踩/从下反弹”
    confirm_mode = str(b.get("confirm_mode", "close")).lower()  # close: 只看收阳/收阴
    # 趋势参数
    htf_fast = int(b.get("htf_fast", 50))
    htf_slow = int(b.get("htf_slow", 200))
    # 入场均线
    entry_ma1 = int(b.get("entry_ma1", 20))
    entry_ma2 = int(b.get("entry_ma2", 30))
    guard_ma = int(b.get("guard_ma", 50))

    # 可选：B也允许用 RSI/ATR 做“不过热/不过冷/不低波动”过滤
    use_rsi_filter = bool(b.get("use_rsi_filter", True))
    use_atr_filter = bool(b.get("use_atr_filter", True))

    opens, highs, lows, closes = extract_ohlc(klines)
    htf_closes = extract_closes(htf_klines)

    if len(closes) < max(guard_ma, entry_ma2) + 5:
        debug_info["reason"] = "B_not_enough_ltf"
        return 0

    trend, trend_info = detect_trend_ma50_200(htf_closes, fast=htf_fast, slow=htf_slow)
    debug_info.update(trend_info)
    if trend == "none":
        debug_info["reason"] = "B_trend_none"
        return 0

    ema20 = ema(closes, entry_ma1)[-1]
    ema30 = ema(closes, entry_ma2)[-1]
    ema50 = ema(closes, guard_ma)[-1]

    # 回踩发生在倒数第二根，确认在最后一根
    o_prev, c_prev = opens[-2], closes[-2]
    o_now, c_now = opens[-1], closes[-1]
    c_before = closes[-3]

    # “接近 EMA20/30”
    near_ma = (pct_diff(c_prev, ema20) <= pull_near_pct) or (pct_diff(c_prev, ema30) <= pull_near_pct)

    # 守卫（不有效跌破/站上 EMA50）
    if trend == "bull":
        guard_line = ema50 * (1 - guard_break_pct)
        guard_ok = c_prev >= guard_line
        confirm_ok = (c_now > o_now) if confirm_mode == "close" else (c_now > c_prev)
        structure_ok = (c_before > c_prev) if require_structure else True

        if not (near_ma and guard_ok and confirm_ok and structure_ok):
            debug_info["reason"] = "B_no_setup_long"
            debug_info["B_near_ma"] = near_ma
            debug_info["B_guard_ok"] = guard_ok
            debug_info["B_confirm_ok"] = confirm_ok
            debug_info["B_structure_ok"] = structure_ok
            return 0

        # B 多信号
        debug_info["signal_name"] = "B_pullback_reentry"
        debug_info["signal_reason"] = "4H多头趋势 + 15m回踩EMA20/30 + 未破EMA50守卫 + 收阳确认"
        debug_info["B_trend"] = "bull"
        debug_info["B_ema20"] = ema20
        debug_info["B_ema30"] = ema30
        debug_info["B_ema50"] = ema50
        debug_info["B_pullback_close"] = c_prev
        debug_info["B_confirm_close"] = c_now

        # 可选过滤：RSI/ATR（沿用你原 filters 的参数）
        if use_rsi_filter:
            rsi_period = int(filters_cfg.get("rsi_period", 14))
            rsi_vals = rsi(closes, rsi_period)
            last_rsi = rsi_vals[-1] if rsi_vals else None
            debug_info["rsi"] = last_rsi
            long_overbought = float(filters_cfg.get("rsi_long_overbought", 80))
            if last_rsi is not None and last_rsi >= long_overbought:
                debug_info["blocked_by_rsi"] = "B_long_blocked_overbought"
                return 0

        if use_atr_filter:
            atr_period = int(filters_cfg.get("atr_period", 14))
            atr_min_pct = float(filters_cfg.get("atr_min_pct", 0.002))
            atr_val = atr_from_closes(closes, atr_period)
            atr_pct = (atr_val / closes[-1]) if (atr_val is not None and closes[-1] != 0) else None
            debug_info["atr"] = atr_val
            debug_info["atr_pct"] = atr_pct
            if atr_pct is not None and atr_pct < atr_min_pct:
                debug_info["blocked_by_atr"] = "B_low_vol"
                return 0

        return 1

    else:  # bear
        guard_line = ema50 * (1 + guard_break_pct)
        guard_ok = c_prev <= guard_line
        confirm_ok = (c_now < o_now) if confirm_mode == "close" else (c_now < c_prev)
        structure_ok = (c_before < c_prev) if require_structure else True

        if not (near_ma and guard_ok and confirm_ok and structure_ok):
            debug_info["reason"] = "B_no_setup_short"
            debug_info["B_near_ma"] = near_ma
            debug_info["B_guard_ok"] = guard_ok
            debug_info["B_confirm_ok"] = confirm_ok
            debug_info["B_structure_ok"] = structure_ok
            return 0

        debug_info["signal_name"] = "B_pullback_reentry"
        debug_info["signal_reason"] = "4H空头趋势 + 15m反弹至EMA20/30 + 未站上EMA50守卫 + 收阴确认"
        debug_info["B_trend"] = "bear"
        debug_info["B_ema20"] = ema20
        debug_info["B_ema30"] = ema30
        debug_info["B_ema50"] = ema50
        debug_info["B_pullback_close"] = c_prev
        debug_info["B_confirm_close"] = c_now

        if use_rsi_filter:
            rsi_period = int(filters_cfg.get("rsi_period", 14))
            rsi_vals = rsi(closes, rsi_period)
            last_rsi = rsi_vals[-1] if rsi_vals else None
            debug_info["rsi"] = last_rsi
            short_oversold = float(filters_cfg.get("rsi_short_oversold", 20))
            if last_rsi is not None and last_rsi <= short_oversold:
                debug_info["blocked_by_rsi"] = "B_short_blocked_oversold"
                return 0

        if use_atr_filter:
            atr_period = int(filters_cfg.get("atr_period", 14))
            atr_min_pct = float(filters_cfg.get("atr_min_pct", 0.002))
            atr_val = atr_from_closes(closes, atr_period)
            atr_pct = (atr_val / closes[-1]) if (atr_val is not None and closes[-1] != 0) else None
            debug_info["atr"] = atr_val
            debug_info["atr_pct"] = atr_pct
            if atr_pct is not None and atr_pct < atr_min_pct:
                debug_info["blocked_by_atr"] = "B_low_vol"
                return 0

        return -1


# ---------- 对外主函数 ----------

def generate_signal(
    symbol: str,
    klines: List[List[Any]],
    cfg: Dict[str, Any],
    htf_klines: Optional[List[List[Any]]] = None,
    debug: bool = False,
) -> Tuple[int, Dict[str, Any]]:
    debug_info: Dict[str, Any] = {"symbol": symbol}

    logic = cfg.get("logic", {})
    mode = str(logic.get("mode", "A")).upper()   # "A"(默认原逻辑) / "B"(回踩二次入场)

    # B 必须要有 htf_klines
    if mode == "B":
        if not htf_klines:
            debug_info["reason"] = "B_missing_htf_klines"
            if debug:
                print(f"[DEBUG][strategy] {symbol} -> B missing htf_klines")
            return 0, debug_info

        signal = generate_signal_b_pullback(symbol, klines, htf_klines, cfg, debug_info)
        debug_info["final_signal"] = signal
        if debug:
            print(f"[DEBUG][strategy][B] {symbol} -> final={signal}, info={debug_info}")
        return signal, debug_info

    # ====== 原 A 逻辑（不动你原来的结构） ======
    closes = extract_closes(klines)
    if len(closes) < 10:
        debug_info["reason"] = "not_enough_data"
        if debug:
            print(f"[DEBUG][strategy] {symbol} -> raw=0, info={debug_info}")
        return 0, debug_info

    filters_cfg = cfg.get("filters", {})

    fast = int(logic.get("fast", 12))
    slow = int(logic.get("slow", 26))
    sig = int(logic.get("sig", 9))

    raw_signal, base_info = base_signal_from_ma(closes, fast, slow, sig)
    debug_info.update(base_info)
    debug_info["raw_signal"] = raw_signal
    debug_info["fast"] = fast
    debug_info["slow"] = slow
    debug_info["sig"] = sig

    if raw_signal == 0:
        debug_info["reason"] = "ma_flat"
        if debug:
            print(f"[DEBUG][strategy] {symbol} -> raw=0, info={debug_info}")
        return 0, debug_info

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
