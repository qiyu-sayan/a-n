"""
strategy.py

职责：
- 根据 K 线数据 + 配置，生成交易信号
- 分两层：
  1）基础信号层（双均线 + 信号线） -> raw_signal ∈ {-1, 0, 1}
  2）过滤层（趋势过滤 + RSI + 暴跌保护） -> final_signal ∈ {-1, 0, 1}

说明：
- 本文件不直接下单，只负责“要不要做多/做空/观望”的决策。
- 可训练参数：cfg["logic"] & cfg["risk"]（交给训练器调）
- 手动配置参数：cfg["filters"]（只在这里使用，训练器不改）
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


# ---------- 技术指标基础实现（不依赖第三方库） ----------

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


def rsi(series: List[float], period: int) -> List[float]:
    """
    标准 Wilder RSI 实现。
    返回与 series 等长的 RSI 列表，前 period 值用 None 填充。
    """
    n = len(series)
    if n == 0 or period <= 0 or n < period + 1:
        return []

    deltas = [series[i] - series[i - 1] for i in range(1, n)]
    gains = [max(d, 0.0) for d in deltas]
    losses = [max(-d, 0.0) for d in deltas]

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    rsi_vals: List[Optional[float]] = [None] * (period + 1)  # 前 period+1 个位置对齐
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

    # 对齐长度
    while len(rsi_vals) < n:
        rsi_vals.insert(0, None)

    return rsi_vals[:n]


# ---------- ① 基础信号层：双均线交叉 ----------

def base_signal_from_ma(
    closes: List[float],
    fast: int,
    slow: int,
    sig: int,  # 目前暂时没用到信号线，可以为以后扩展预留
) -> int:
    """
    返回原始信号 raw_signal ∈ {-1, 0, 1}
    逻辑：双均线“金叉/死叉”给出一次性信号（避免每根K线都重复开仓）

    - fast 从下向上穿 slow：+1（看多）
    - fast 从上向下穿 slow：-1（看空）
    - 其他情况：0（不发新信号）
    """
    if len(closes) < slow + 2:
        return 0

    fast_ema = ema(closes, fast)
    slow_ema = ema(closes, slow)

    # 只看最后两根，用于判断是否发生了“穿越”
    fast_prev, fast_last = fast_ema[-2], fast_ema[-1]
    slow_prev, slow_last = slow_ema[-2], slow_ema[-1]

    prev_diff = fast_prev - slow_prev
    last_diff = fast_last - slow_last

    # 金叉：由下到上
    if prev_diff <= 0 and last_diff > 0:
        return 1

    # 死叉：由上到下
    if prev_diff >= 0 and last_diff < 0:
        return -1

    return 0


# ---------- ② 过滤层：趋势 / RSI / 暴跌 ----------

def detect_htf_trend(
    htf_closes: List[float],
    ma_period: int,
) -> str:
    """
    高周期趋势判断：
    - last_close > MA → "up"
    - last_close < MA → "down"
    - 否则 → "flat"
    """
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
    """
    暴跌检测：
    - 最近 lookback 根K线的整体跌幅 <= threshold（如 -0.03）
    """
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
    """
    过滤逻辑：
    - 高周期趋势：
        * 趋势向下时禁止做多
        * 趋势向上时禁止做空
    - RSI 过滤：
        * RSI > long_overbought → 禁止做多（避免追高）
        * RSI < short_oversold → 禁止做空（避免抄底抄到半山腰）
    - 暴跌过滤：
        * 近期跌幅超过 crash_threshold 时，禁止做多，只保留做空信号
    """

    # 默认返回原始信号
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

    long_overbought = float(cfg_filters.get("rsi_long_overbought", 70))
    short_oversold = float(cfg_filters.get("rsi_short_oversold", 30))

    if last_rsi is not None:
        if final_signal == 1 and last_rsi >= long_overbought:
            debug_info["blocked_by_rsi"] = "long_blocked_overbought"
            final_signal = 0
        elif final_signal == -1 and last_rsi <= short_oversold:
            debug_info["blocked_by_rsi"] = "short_blocked_oversold"
            final_signal = 0

    # --- 3）暴跌过滤 ---
    crash_lookback = int(cfg_filters.get("crash_lookback", 4))
    crash_threshold = float(cfg_filters.get("crash_threshold", -0.03))
    crashed = detect_crash(closes, crash_lookback, crash_threshold)
    debug_info["crashed"] = crashed

    if crashed and final_signal == 1:
        # 暴跌后禁止立即做多，只允许做空或观望
        debug_info["blocked_by_crash"] = "long_blocked_after_crash"
        final_signal = 0

    return final_signal


# ---------- 对外主函数：生成信号 ----------

def generate_signal(
    symbol: str,
    klines: List[List[Any]],
    cfg: Dict[str, Any],
    htf_klines: Optional[List[List[Any]]] = None,
    debug: bool = False,
) -> Tuple[int, Dict[str, Any]]:
    """
    主入口：

    参数：
        symbol     : 交易对，例如 "BTCUSDT"
        klines     : 当前周期 K 线（如 1h），列表形式
        cfg        : 完整配置（包含 logic / filters 等）
        htf_klines : 高周期 K 线（如 4h），可选
        debug      : True 时会在 debug_info 里写入更多细节

    返回：
        (signal, debug_info)
        signal ∈ {-1, 0, 1}:
            1  -> 做多
            -1 -> 做空
            0  -> 不下单
    """
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
    raw_signal = base_signal_from_ma(closes, fast, slow, sig)
    debug_info["raw_signal"] = raw_signal
    debug_info["fast"] = fast
    debug_info["slow"] = slow
    debug_info["sig"] = sig

    if raw_signal == 0:
        # 没有新金叉/死叉信号，直接退出（也可以选择进入过滤层再看）
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
