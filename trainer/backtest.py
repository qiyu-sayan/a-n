# trainer/backtest.py
# -*- coding: utf-8 -*-
import math, time, json
from statistics import mean

def equity_curve(trades, fee_rate=0.0004, slippage=0.0002):
    """简化的权益曲线：trades: [(ts, side, price)]，用固定1单位名义仓模拟相对收益"""
    eq = [1.0]; pos = 0   # 1代表初始资金
    entry_price = None
    for ts, side, price in trades:
        px = price * (1+slippage if side=="BUY" else 1-slippage)
        if side=="BUY" and pos==0:
            entry_price = px; pos = 1
            eq.append(eq[-1] * (1-fee_rate))
        elif side=="SELL" and pos==1:
            ret = (px - entry_price)/entry_price
            eq.append(eq[-1] * (1 + ret) * (1-fee_rate))
            pos=0; entry_price=None
    return eq

def metrics_from_equity(eq):
    pnl = eq[-1]-1
    peaks = []; mdd=0; peak=eq[0]
    for v in eq:
        peak=max(peak,v); dd=(peak-v)/peak; mdd=max(mdd,dd)
    # 粗略夏普/索提诺（以步进为“天”代指）
    rets=[(eq[i]/eq[i-1]-1) for i in range(1,len(eq))]
    if not rets: return {"pnl":pnl,"maxdd":mdd,"sharpe":0,"sortino":0}
    mu = mean(rets); downside=[r for r in rets if r<0]
    sd = (mean([(r-mu)**2 for r in rets])**0.5) or 1e-9
    sdr= (mean([d**2 for d in downside])**0.5) or 1e-9
    sharpe = mu/sd
    sortino= mu/sdr
    return {"pnl":pnl,"maxdd":mdd,"sharpe":sharpe,"sortino":sortino}