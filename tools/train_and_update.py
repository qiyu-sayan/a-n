#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
训练脚本：抓取K线 → 回测多组参数 → 比较表现 → 如有显著提升则写回 config/params.json
适配 main.py 中的两套策略：sma_rsi / mean_revert
仅使用 Binance 公共接口，无需 API KEY
"""

import os, json, time, math, statistics, sys
from datetime import datetime, timedelta, timezone
from typing import Dict, Any, List, Tuple
import requests
import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(ROOT, "config", "params.json")

BINANCE_BASE = "https://api.binance.com"  # 正式网
# 可以按需切 testnet 公开行情，注意并非所有 symbol 都有完全行情镜像
# BINANCE_BASE = "https://testnet.binance.vision"

UTC = timezone.utc

def load_cfg() -> Dict[str, Any]:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)

def save_cfg(cfg: Dict[str, Any]):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
        f.write("\n")

def rsi(series: pd.Series, length: int = 14) -> pd.Series:
    delta = series.diff()
    gain = (delta.clip(lower=0)).ewm(alpha=1/length, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1/length, adjust=False).mean()
    rs = gain / (loss.replace(0, np.nan))
    return 100 - (100 / (1 + rs))

def fetch_klines(symbol: str, interval: str, lookback_hours: int) -> pd.DataFrame:
    # Binance 单次最大 1000 根；按小时估算需要的根数
    need = max(100, min(1000, lookback_hours * 60 // _interval_minutes(interval) + 50))
    url = f"{BINANCE_BASE}/api/v3/klines"
    params = dict(symbol=symbol, interval=interval, limit=need)
    r = requests.get(url, params=params, timeout=15)
    r.raise_for_status()
    arr = r.json()
    cols = ['open_time','open','high','low','close','volume','close_time','qv','trades','tb_base','tb_quote','ignore']
    df = pd.DataFrame(arr, columns=cols)
    df['open_time'] = pd.to_datetime(df['open_time'], unit='ms', utc=True)
    df['close_time'] = pd.to_datetime(df['close_time'], unit='ms', utc=True)
    for c in ['open','high','low','close','volume']:
        df[c] = df[c].astype(float)
    return df

def _interval_minutes(interval: str) -> int:
    # 简易换算
    unit = interval[-1].lower()
    n = int(interval[:-1])
    if unit == 'm': return n
    if unit == 'h': return n * 60
    if unit == 'd': return n * 60 * 24
    raise ValueError(f"Unsupported interval: {interval}")

def metric_sortino(returns: List[float]) -> float:
    if not returns:
        return 0.0
    mean = np.mean(returns)
    downside = np.std([min(0, r) for r in returns]) or 1e-9
    return mean / downside

def metric_sharpe(returns: List[float]) -> float:
    if not returns:
        return 0.0
    std = np.std(returns) or 1e-9
    return np.mean(returns) / std

def max_drawdown(equity: List[float]) -> float:
    peak = -1e18
    dd = 0.0
    for x in equity:
        if x > peak:
            peak = x
        dd = min(dd, (x - peak) / peak if peak > 0 else 0.0)
    return abs(dd)

def backtest_sma_rsi(
    df: pd.DataFrame,
    cash0: float,
    fee: float,
    slip: float,
    sma_fast: int,
    sma_slow: int,
    rsi_len: int,
    rsi_buy_below: float,
    rsi_sell_above: float,
    stop_loss_pct: float,
    take_profit_pct: float,
) -> Dict[str, float]:
    d = df.copy()
    d['sma_fast'] = d['close'].rolling(sma_fast).mean()
    d['sma_slow'] = d['close'].rolling(sma_slow).mean()
    d['rsi'] = rsi(d['close'], rsi_len)
    d = d.dropna().reset_index(drop=True)

    pos = 0.0    # 持仓数量（以“币”为单位）
    cash = cash0
    entry_price = 0.0
    equity_curve = []
    rets = []

    for i, row in d.iterrows():
        price = float(row['close'])
        # 滑点
        buy_price  = price * (1 + slip)
        sell_price = price * (1 - slip)

        # 止损 / 止盈
        if pos > 0:
            if stop_loss_pct > 0 and (price <= entry_price * (1 - stop_loss_pct)):
                cash += pos * sell_price * (1 - fee)
                pos = 0
            elif take_profit_pct > 0 and (price >= entry_price * (1 + take_profit_pct)):
                cash += pos * sell_price * (1 - fee)
                pos = 0

        # 信号
        up_trend = row['sma_fast'] > row['sma_slow']
        if pos == 0 and up_trend and row['rsi'] < rsi_buy_below:
            # 用固定现金买
            usdt_to_use = min(cash, 1000000)
            if usdt_to_use > 0:
                qty = (usdt_to_use / buy_price) * (1 - fee)
                pos += qty
                cash -= usdt_to_use
                entry_price = buy_price

        elif pos > 0 and ((not up_trend) or row['rsi'] > rsi_sell_above):
            cash += pos * sell_price * (1 - fee)
            pos = 0

        equity = cash + pos * price
        equity_curve.append(equity)

        if i > 0:
            rets.append((equity_curve[-1] - equity_curve[-2]) / max(equity_curve[-2], 1e-9))

    pnl = (equity_curve[-1] - cash0) / cash0 if equity_curve else 0.0
    dd = max_drawdown(equity_curve) if equity_curve else 0.0
    sharpe = metric_sharpe(rets)
    sortino = metric_sortino(rets)

    return dict(pnl=pnl, dd=dd, sharpe=sharpe, sortino=sortino)

def backtest_mean_revert(
    df: pd.DataFrame,
    cash0: float,
    fee: float,
    slip: float,
    win_std: int,
    z_entry: float,
    z_exit: float,
    stop_loss_pct: float,
    take_profit_pct: float,
) -> Dict[str, float]:
    d = df.copy()
    d['ma'] = d['close'].rolling(win_std).mean()
    d['std'] = d['close'].rolling(win_std).std()
    d = d.dropna().reset_index(drop=True)

    pos = 0.0
    cash = cash0
    entry_price = 0.0
    equity_curve, rets = [], []

    for i, row in d.iterrows():
        price = float(row['close'])
        buy_price  = price * (1 + slip)
        sell_price = price * (1 - slip)

        z = (price - row['ma']) / (row['std'] or 1e-9)

        if pos > 0:
            if stop_loss_pct > 0 and (price <= entry_price * (1 - stop_loss_pct)):
                cash += pos * sell_price * (1 - fee)
                pos = 0
            elif take_profit_pct > 0 and (price >= entry_price * (1 + take_profit_pct)):
                cash += pos * sell_price * (1 - fee)
                pos = 0

        # 低于入场阈值买入，高于离场阈值卖出
        if pos == 0 and z <= -abs(z_entry):
            usdt_to_use = min(cash, 1000000)
            if usdt_to_use > 0:
                qty = (usdt_to_use / buy_price) * (1 - fee)
                pos += qty
                cash -= usdt_to_use
                entry_price = buy_price
        elif pos > 0 and z >= abs(z_exit):
            cash += pos * sell_price * (1 - fee)
            pos = 0

        equity = cash + pos * price
        equity_curve.append(equity)
        if i > 0:
            rets.append((equity_curve[-1] - equity_curve[-2]) / max(equity_curve[-2], 1e-9))

    pnl = (equity_curve[-1] - cash0) / cash0 if equity_curve else 0.0
    dd = max_drawdown(equity_curve) if equity_curve else 0.0
    sharpe = metric_sharpe(rets)
    sortino = metric_sortino(rets)
    return dict(pnl=pnl, dd=dd, sharpe=sharpe, sortino=sortino)

def score(obj: str, m: Dict[str, float]) -> float:
    if obj == "sortino": return m["sortino"]
    if obj == "sharpe":  return m["sharpe"]
    return m["pnl"]  # 默认

def main():
    cfg = load_cfg()
    syms: List[str] = cfg.get("symbols", ["BTCUSDT"])
    interval: str = cfg.get("interval", "1m")
    risk = cfg.get("risk", {})
    fee = float(risk.get("fee_rate", 0.0004))
    slip = float(risk.get("slippage", 0.0002))
    stop_loss_pct = float(risk.get("stop_loss_pct", 0.02))
    take_profit_pct = float(risk.get("take_profit_pct", 0.04))
    trainer = cfg.get("trainer", {})
    lookback_h = int(trainer.get("lookback_hours", 72))
    objective = trainer.get("objective", "sortino")
    min_improve = float(trainer.get("min_improve_pct", 0.02))
    strategy = cfg.get("strategy", "sma_rsi")
    params = cfg.get("params", {})
    seed_cash = 1000.0

    print(f"[INFO] symbols={syms}, interval={interval}, strategy={strategy}, objective={objective}")

    # 聚合多币对的成绩取平均（简单起见）
    def eval_params_sma_rsi(pa: Dict[str, Any]) -> float:
        scores = []
        for sym in syms:
            df = fetch_klines(sym, interval, lookback_h)
            m = backtest_sma_rsi(
                df, seed_cash, fee, slip,
                pa["sma_fast"], pa["sma_slow"], pa["rsi_len"],
                pa["rsi_buy_below"], pa["rsi_sell_above"],
                stop_loss_pct, take_profit_pct
            )
            scores.append(score(objective, m))
        return float(np.mean(scores)) if scores else -1e9

    def eval_params_mean_revert(pa: Dict[str, Any]) -> float:
        scores = []
        for sym in syms:
            df = fetch_klines(sym, interval, lookback_h)
            m = backtest_mean_revert(
                df, seed_cash, fee, slip,
                pa["win_std"], pa["z_entry"], pa["z_exit"],
                stop_loss_pct, take_profit_pct
            )
            scores.append(score(objective, m))
        return float(np.mean(scores)) if scores else -1e9

    best_params = params.copy()
    if strategy == "sma_rsi":
        base = {
            "sma_fast": int(params.get("sma_fast", 12)),
            "sma_slow": int(params.get("sma_slow", 26)),
            "rsi_len":  int(params.get("rsi_len", 14)),
            "rsi_buy_below": float(params.get("rsi_buy_below", 55)),
            "rsi_sell_above": float(params.get("rsi_sell_above", 45)),
        }
        base_score = eval_params_sma_rsi(base)
        print(f"[BASE] {base} -> {objective}={base_score:.4f}")

        # 局部网格（围绕当前参数微调）
        grid = []
        for fa in range(max(5, base["sma_fast"]-4), base["sma_fast"]+5, 2):
            for sl in range(max(fa+1, base["sma_slow"]-10), base["sma_slow"]+11, 4):
                for rl in [max(8, base["rsi_len"]-4), base["rsi_len"], base["rsi_len"]+4]:
                    for rb in [base["rsi_buy_below"]-5, base["rsi_buy_below"], base["rsi_buy_below"]+5]:
                        for rs in [base["rsi_sell_above"]-5, base["rsi_sell_above"], base["rsi_sell_above"]+5]:
                            if sl <= fa:   # 确保慢线>快线
                                continue
                            grid.append(dict(sma_fast=fa, sma_slow=sl, rsi_len=rl,
                                             rsi_buy_below=max(10, min(90, rb)),
                                             rsi_sell_above=max(10, min(90, rs))))
        best_score = base_score
        for g in grid:
            sc = eval_params_sma_rsi(g)
            if sc > best_score:
                best_score, best_params = sc, g
        improve = (best_score - base_score) / (abs(base_score) + 1e-9)
        print(f"[BEST] {best_params} -> {objective}={best_score:.4f}, improve={improve:.2%}")

        if improve >= min_improve:
            cfg["params"] = best_params
            save_cfg(cfg)
            print("[WRITE] params.json updated")
        else:
            print("[KEEP] no significant improvement")

    elif strategy == "mean_revert":
        base = {
            "win_std": int(params.get("win_std", 20)),
            "z_entry": float(params.get("z_entry", 1.0)),
            "z_exit":  float(params.get("z_exit", 0.3)),
        }
        base_score = eval_params_mean_revert(base)
        print(f"[BASE] {base} -> {objective}={base_score:.4f}")

        grid = []
        for ws in [max(10, base["win_std"]-10), base["win_std"], base["win_std"]+10]:
            for ze in [0.8, 1.0, 1.2, 1.5]:
                for zx in [0.2, 0.3, 0.5, 0.8]:
                    if zx >= ze:  # 离场阈值应小于入场阈值
                        continue
                    grid.append(dict(win_std=ws, z_entry=ze, z_exit=zx))

        best_score = base_score
        for g in grid:
            sc = eval_params_mean_revert(g)
            if sc > best_score:
                best_score, best_params = sc, g
        improve = (best_score - base_score) / (abs(base_score) + 1e-9)
        print(f"[BEST] {best_params} -> {objective}={best_score:.4f}, improve={improve:.2%}")

        if improve >= min_improve:
            cfg["params"] = best_params
            save_cfg(cfg)
            print("[WRITE] params.json updated")
        else:
            print("[KEEP] no significant improvement")
    else:
        print(f"[ERROR] Unsupported strategy: {strategy}")
        sys.exit(1)

if __name__ == "__main__":
    main()