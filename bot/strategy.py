import numpy as np


def ema(arr, period: int):
    arr = np.array(arr, dtype=float)
    if len(arr) < period:
        return None
    alpha = 2 / (period + 1)
    ema_val = arr[0]
    for v in arr[1:]:
        ema_val = alpha * v + (1 - alpha) * ema_val
    return ema_val


def rsi(arr, period: int = 14):
    arr = np.array(arr, dtype=float)
    if len(arr) < period + 1:
        return None
    diff = np.diff(arr)
    up = diff.clip(min=0)
    down = -diff.clip(max=0)
    avg_gain = np.mean(up[-period:])
    avg_loss = np.mean(down[-period:])
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def macd(arr, fast=12, slow=26, signal=9):
    arr = np.array(arr, dtype=float)
    if len(arr) < slow + signal:
        return None, None
    ema_fast = ema(arr, fast)
    ema_slow = ema(arr, slow)
    if ema_fast is None or ema_slow is None:
        return None, None
    macd_line = ema_fast - ema_slow
    return macd_line, 0.0


def _extract_close(klines):
    return [float(k[4]) for k in klines]


def generate_signal(
    symbol: str,
    klines: list,
    cfg: dict,
    htf_klines: list | None = None,
    debug: bool = False,
):
    """
    返回：
      signal: 'LONG' | 'SHORT' | None
      info: dict（结构化信号说明）
    """
    info = {
        "symbol": symbol,
        "trend": None,
        "entry": None,
        "reason": None,
        "ema": None,
        "rsi": None,
        "macd": None,
        "score": 0,
    }

    closes = _extract_close(klines)
    if len(closes) < 30:
        info["reason"] = "KLINE_TOO_SHORT"
        return None, info

    # === 1. 大周期趋势判断（4H） ===
    if htf_klines:
        htf_closes = _extract_close(htf_klines)
        ema20_htf = ema(htf_closes, 20)
        ema50_htf = ema(htf_closes, 50)

        if ema20_htf and ema50_htf:
            if ema20_htf > ema50_htf:
                info["trend"] = "UP"
                info["score"] += 1
            elif ema20_htf < ema50_htf:
                info["trend"] = "DOWN"
                info["score"] += 1
            else:
                info["trend"] = "FLAT"
        else:
            info["trend"] = "UNKNOWN"

    if info["trend"] not in ("UP", "DOWN"):
        info["reason"] = "NO_TREND"
        return None, info

    # === 2. 小周期结构判断（15m） ===
    ema5 = ema(closes, 5)
    ema10 = ema(closes, 10)
    ema20 = ema(closes, 20)

    if not all([ema5, ema10, ema20]):
        info["reason"] = "EMA_NOT_READY"
        return None, info

    info["ema"] = "EMA5<EMA10<EMA20" if ema5 < ema10 < ema20 else "EMA5>EMA10>EMA20"

    rsi_val = rsi(closes)
    info["rsi"] = round(rsi_val, 2) if rsi_val else None

    macd_line, _ = macd(closes)
    info["macd"] = "above_zero" if macd_line and macd_line > 0 else "below_zero"

    # === 3. 入场逻辑（回踩 / 二次确认） ===
    last = closes[-1]

    if info["trend"] == "UP":
        if ema5 > ema10 > ema20 and rsi_val and rsi_val > 45:
            info["entry"] = "PULLBACK_2"
            info["reason"] = "4H uptrend + 15m EMA support"
            info["score"] += 2
            return "LONG", info

    if info["trend"] == "DOWN":
        if ema5 < ema10 < ema20 and rsi_val and rsi_val < 55:
            info["entry"] = "PULLBACK_2"
            info["reason"] = "4H downtrend + 15m EMA rejection"
            info["score"] += 2
            return "SHORT", info

    info["reason"] = "NO_ENTRY"
    return None, info
