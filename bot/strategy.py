"""
strategy.py

职责：
- 根据 K 线数据 + 配置，生成交易信号
- 分两层：
  1）基础信号层（双均线方向） -> raw_signal ∈ {-1, 0, 1}
  2）过滤层（趋势过滤 + RSI + 暴跌 + 波动率 + EMA 斜率） -> final_signal ∈ {-1, 0, 1}

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
    if n == 0 or period <= 0
