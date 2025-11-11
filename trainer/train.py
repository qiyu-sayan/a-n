# trainer/train.py
# -*- coding: utf-8 -*-
import os, json, time, math, itertools, requests, traceback
from datetime import datetime, timedelta, timezone
from bot.wecom_notify import wecom_notify, warn_451
from bot.strategy import route_signal
from trainer.backtest import equity_curve, metrics_from_equity

REST_BASE = "https://api.binance.com"

def get_klines(symbol, interval, start_ts, end_ts, limit=1000):
    """拉取[start,end)区间的K线（简化：直接分页到取完）"""
    out=[]
    url=f"{REST_BASE}/api/v3/klines"
    ts=start_ts
    while True:
        params={"symbol":symbol,"interval":interval,"limit":min(limit,1000)}
        r=requests.get(url,params=params,timeout=15)
        if r.status_code==451:
            warn_451(url); break
        r.raise_for_status()
        data=r.json()
        if not data: break
        out.extend([k for k in data if k[0]>=start_ts and k[0]<end_ts])
        if len(data)<limit: break
        # 简化：这里没有精确翻页（Binance还支持startTime/endTime），够用即可
        break
    return out

def closes_of(kl):
    return [float(k[4]) for k in kl]

def walk_forward_backtest(symbols, interval, params, risk, lookback_hours):
    end  = int(time.time()*1000)
    start= end - lookback_hours*3600*1000
    trades=[]
    for sym in symbols:
        kl=get_klines(sym, interval, start, end, limit=1000)
        c = closes_of(kl)
        for i in range(60,len(c)):  # 从较后起点，避免前段指标不足
            sub=c[:i+1]
            sig=route_signal(params["strategy"], sub, params["params"])
            px=sub[-1]
            if sig in ("BUY","SELL"):
                trades.append((kl[i][0], sig, px))
    eq = equity_curve(trades, fee_rate=risk["fee_rate"], slippage=risk["slippage"])
    return metrics_from_equity(eq)

def grid_candidates(strategy):
    if strategy=="sma_rsi":
        return {
            "sma_fast":[8,12,16],
            "sma_slow":[22,26,30],
            "rsi_len":[10,14,18],
            "rsi_buy_below":[50,55,60],
            "rsi_sell_above":[40,45,50]
        }
    # 备用
    return {"mr_len":[20,30],"mr_buy_z":[-1.0,-1.5],"mr_sell_z":[1.0,1.5]}

def search_best(cfg):
    sym=cfg["symbols"]; iv=cfg["interval"]; risk=cfg["risk"]; trainer=cfg["trainer"]
    cand = grid_candidates(cfg["strategy"])
    keys=list(cand.keys())
    best=None; best_m=None
    total=1
    for k in cand.values(): total*=len(k)
    cnt=0

    for values in itertools.product(*[cand[k] for k in keys]):
        params = {**cfg}
        params["params"] = {**cfg["params"], **{k:v for k,v in zip(keys,values)}}
        m = walk_forward_backtest(sym, iv, params, risk, trainer["lookback_hours"])
        cnt+=1
        # 目标：Sortino最大，且 maxDD <= cap
        if m["maxdd"]<=risk["max_drawdown_cap"]:
            if (best_m is None) or (m[trainer["objective"]]>best_m[trainer["objective"]]):
                best, best_m = params, m

    return best, best_m

def read_params(path="config/params.json"):
    with open(path,"r",encoding="utf-8") as f: return json.load(f)

def write_params(cfg, path="config/params.json"):
    with open(path,"w",encoding="utf-8") as f: json.dump(cfg,f,ensure_ascii=False,indent=2)

def main():
    cfg=read_params()

    # 条件：最近表现很差时强制降档
    # （这里简单用回测替代“真实近7天PNL”，后续可从你日志里汇总）
    m_now = walk_forward_backtest(cfg["symbols"], cfg["interval"], cfg, cfg["risk"], 24)
    bad = (m_now["pnl"] <= cfg["trainer"]["retrain_if_7d_pnl_below"]) or (m_now["maxdd"] >= cfg["trainer"]["retrain_if_dd_over"])
    wecom_notify(f"🧪 当前参数 24h 估算：pnl={m_now['pnl']:.3f}, dd={m_now['maxdd']:.3f}, sortino={m_now['sortino']:.2f}")

    # 训练/搜索
    best, best_m = search_best(cfg)
    if not best:
        wecom_notify("❌ 训练未找到满足回撤约束的参数，保持现状")
        return

    # 对比是否足够改写
    if best_m[cfg["trainer"]["objective"]] >= m_now[cfg["trainer"]["objective"]] * (1 + cfg["trainer"]["min_improve_pct"]):
        # 若近期很差，且新参数也达不到阈值，则降档
        if bad and best_m["maxdd"] > cfg["risk"]["max_drawdown_cap"]*0.9:
            best["mode"]="paper"
            best["order_usdt"]=max(5, int(best["order_usdt"]*0.5))
            wecom_notify("⚠️ 触发风控：切换纸交易并下调仓位")
        write_params(best)
        wecom_notify(
            "✅ 已更新参数并写回仓库\n"
            f"策略: {best['strategy']}  symbols:{best['symbols']}  interval:{best['interval']}\n"
            f"目标({cfg['trainer']['objective']}): {best_m[cfg['trainer']['objective']]:.3f}  "
            f"pnl:{best_m['pnl']:.3f}  dd:{best_m['maxdd']:.3f}"
        )
    else:
        wecom_notify("ℹ️ 新参数提升不足，保持现状")

if __name__=="__main__":
    try:
        main()
    except Exception as e:
        wecom_notify(f"❌ 训练进程异常：{e}\n{traceback.format_exc()}")
        raise