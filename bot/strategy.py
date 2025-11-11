# bot/strategy.py
# -*- coding: utf-8 -*-
import json, os, math
from statistics import mean

def load_params(path="config/params.json"):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def sma(series, n):
    if len(series) < n: return None
    return sum(series[-n:]) / n

def rsi(closes, n=14):
    if len(closes) <= n: return None
    gains, losses = [], []
    for i in range(1, n+1):
        chg = closes[-i] - closes[-i-1]
        if chg >= 0: gains.append(chg)
        else: losses.append(-chg)
    avg_gain = sum(gains)/n if gains else 0.0
    avg_loss = sum(losses)/n if losses else 0.0
    if avg_loss == 0: return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100/(1+rs))

def signal_sma_rsi(closes, p):
    fast = p["sma_fast"]; slow = p["sma_slow"]
    rlen = p["rsi_len"];  rb = p["rsi_buy_below"]; rs = p["rsi_sell_above"]

    if len(closes) < max(slow, rlen) + 2: return "HOLD"

    sma_fast_prev = sma(closes[:-1], fast); sma_slow_prev = sma(closes[:-1], slow)
    sma_fast_now  = sma(closes, fast);      sma_slow_now  = sma(closes, slow)
    r = rsi(closes, rlen)

    cross_up   = sma_fast_prev <= sma_slow_prev and sma_fast_now > sma_slow_now
    cross_down = sma_fast_prev >= sma_slow_prev and sma_fast_now < sma_slow_now

    if cross_up and (r is None or r <= rb):
        return "BUY"
    if cross_down and (r is None or r >= rs):
        return "SELL"
    return "HOLD"

def signal_mean_revert(closes, p):
    # 备用：价格偏离均线Z-score阈值做反转
    n = p.get("mr_len", 20)
    if len(closes) < n+2: return "HOLD"
    mu = mean(closes[-n:])
    std = (sum((x-mu)**2 for x in closes[-n:])/n)**0.5 or 1e-9
    z = (closes[-1]-mu)/std
    buy_z = p.get("mr_buy_z", -1.0)
    sell_z= p.get("mr_sell_z", +1.0)
    if z <= buy_z:  return "BUY"
    if z >= sell_z: return "SELL"
    return "HOLD"

def route_signal(strategy_name, closes, params):
    if strategy_name == "sma_rsi":
        return signal_sma_rsi(closes, params)
    elif strategy_name == "mean_revert":
        return signal_mean_revert(closes, params)
    return "HOLD"