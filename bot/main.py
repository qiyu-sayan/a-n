# bot/main.py
from __future__ import annotations

import csv
import os
from datetime import datetime, timedelta
from typing import List, Tuple, Optional

import ccxt

from bot.strategy import generate_orders
from bot.wecom_notify import send_wecom_markdown
from bot.trader import (
    Trader,
    OrderRequest,
    MarketType,
    PositionSide,
)

# 运行环境：test（模拟） / live（实盘）
class Env:
    TEST = "test"
    LIVE = "live"


def now_ts() -> str:
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")


def _estimate_notional(ex: ccxt.Exchange, market: MarketType, symbol: str, amount: float) -> float:
    """
    尝试预估名义金额 = amount * 最新价格。
    模拟盘很多字段拿不到，这里尽量多个字段兜底。
    """
    try:
        m = ex.market(symbol)
        info = m.get("info", {})

        price = None

        # 先尝试 ticker
        t = ex.fetch_ticker(symbol)
        price = (
            t.get("last")
            or t.get("close")
            or t.get("ask")
            or t.get("bid")
        )

        # 再尝试 info 里的字段
        if not price:
            price = info.get("last") or info.get("idxPx") or info.get("markPx")

        # 完全失败则返回 0
        if not price:
            return 0.0

        return abs(float(amount)) * float(price)

    except Exception:
        return 0.0


# DEMO 默认每笔的固定名义金额（USDT）
ORDER_USDT = float(os.getenv("ORDER_USDT", "10"))

# LIVE 风控
MAX_ORDERS_PER_RUN = 3
MAX_FUTURES_NOTIONAL_LIVE = 50_000
LIVE_RISK_PER_TRADE = 0.05  # 5%


# DEMO 风控：只按固定金额，不用余额
MAX_FUTURES_NOTIONAL_TEST = ORDER_USDT


def _apply_risk_controls(
    env: str,
    spot_ex: ccxt.Exchange,
    fut_ex: ccxt.Exchange,
    orders: List[OrderRequest],
) -> List[Tuple[OrderRequest, float]]:
    """
    返回：[(订单, 预估名义金额)]
    DEMO：无余额检查，只用固定上限裁剪
    LIVE：严格按余额 + 风险比例裁剪
    """
    adjusted: List[Tuple[OrderRequest, float]] = []

    # DEMO 模拟盘金额上限
    demo_cap = MAX_FUTURES_NOTIONAL_TEST

    live_total_usdt = 0.0
    live_free_usdt = 0.0

    # LIVE 账户余额
    if env == Env.LIVE:
        try:
            bal = spot_ex.fetch_balance(params={"type": "trade"})
            usdt = bal.get("USDT") or bal.get("usdt") or {}
            live_total_usdt = float(usdt.get("total") or 0.0)
            live_free_usdt = float(usdt.get("free") or 0.0)
            print(f"[main] 实盘余额: total={live_total_usdt}, free={live_free_usdt}")
        except Exception as e:
            print(f"[main] 获取余额失败: {e}")
            return []

    count = 0

    for req in orders:
        if count >= MAX_ORDERS_PER_RUN:
            print("[risk] 超出每轮最大下单数，忽略剩余信号。")
            break

        ex = fut_ex if req.market == MarketType.FUTURES else spot_ex

        # 预估名义金额
        notional = _estimate_notional(ex, req.market, req.symbol, req.amount)

        # ===== DEMO 模式（模拟盘）：无法估价就直接用固定 cap =====
        if env == Env.TEST:
            if notional <= 0:
                notional = demo_cap
                print(f"[risk] DEMO 无法预估名义金额 → 使用固定名义金额 {notional}。")

            elif notional > demo_cap:
                scale = demo_cap / notional
                old_amount = req.amount
                req.amount *= scale
                notional = demo_cap
                print(
                    f"[risk] DEMO 名义金额超出上限，调仓: {old_amount:.6f} → {req.amount:.6f}"
                )

            adjusted.append((req, notional))
            count += 1
            continue

        # ===== LIVE 模式（实盘）=====
        if notional <= 0:
            print(f"[risk] LIVE 无法预估名义金额，跳过订单。")
            continue

        abs_cap = MAX_FUTURES_NOTIONAL_LIVE
        dyn_cap = live_total_usdt * LIVE_RISK_PER_TRADE
        cap = min(abs_cap, dyn_cap, live_free_usdt)

        if cap <= 0:
            print("[risk] LIVE 可用余额不足，放弃全部下单。")
            return []

        if notional > cap:
            scale = cap / notional
            old_amount = req.amount
            req.amount *= scale
            notional = cap
            print(
                f"[risk] LIVE 名义金额超上限 → 调仓 {old_amount:.6f} → {req.amount:.6f}"
            )

        adjusted.append((req, notional))
        count += 1

    return adjusted


def _log_trade(req: OrderRequest, result: dict, env: str, notional: float):
    """写入日志 CSV"""
    path = f"logs/trades_{env}.csv"
    exists = os.path.exists(path)

    with open(path, "a", newline="", encoding="utf8") as f:
        w = csv.writer(f)
        if not exists:
            w.writerow(
                ["ts", "symbol", "side", "position_side", "amount", "notional", "lev", "order_id"]
            )
        w.writerow(
            [
                now_ts(),
                req.symbol,
                req.side.value,
                req.position_side.value if req.position_side else "",
                req.amount,
                notional,
                req.leverage,
                result.get("order_id", ""),
            ]
        )


def run_once(env: str):
    print(f"[main] 运行环境: {env}")
    if env == Env.TEST:
        print("Notice：当前为 DEMO 模拟盘环境，用于调试策略与风控逻辑，请勿视为实盘。")

    # 创建客户端
    spot_ex = ccxt.okx({
        "apiKey": os.getenv("OKX_PAPER_API_KEY"),
        "secret": os.getenv("OKX_PAPER_API_SECRET"),
        "password": os.getenv("OKX_PAPER_API_PASSPHRASE"),
    })

    fut_ex = spot_ex

    trader = Trader(env, spot_ex, fut_ex)

    # 生成策略信号
    raw_orders = generate_orders(trader)

    if not raw_orders:
        print("[main] 本次无交易信号。")
        return

    # 风控
    adjusted = _apply_risk_controls(env, spot_ex, fut_ex, raw_orders)

    if not adjusted:
        print("[main] 风控后无有效订单，退出。")
        return

    print(f"[main] 策略生成 {len(adjusted)} 个订单。")

    wecom_msgs = []
    success_count = 0

    # 执行下单
    for req, notional in adjusted:
        print(f"[main] (FUT) 下单: {req.symbol} {req.side.value} amount={req.amount}, lev={req.leverage}")

        ok, result = trader.place_order(req)

        if ok:
            success_count += 1
            order_id = result.get("order_id", "")
            msg = (
                f"【成功】{req.symbol} {req.side.value}\n"
                f"数量: {req.amount}\n"
                f"杠杆: {req.leverage}x\n"
                f"notional≈{notional}"
            )
            wecom_msgs.append(msg)
            _log_trade(req, result, env, notional)
        else:
            msg = (
                f"【失败】{req.symbol} {req.side.value}\n"
                f"错误: {result.get('error')}"
            )
            wecom_msgs.append(msg)

        print("[main] 下单结果:", ok, result)

    # 发送企业微信通知
    if wecom_msgs:
        md = "### 交易机器人执行结果 - DEMO (OKX 模拟盘)\n"
        for m in wecom_msgs:
            md += m + "\n\n"
        send_wecom_markdown(md)

    print(f"[main] 本轮完成：成功 {success_count} / {len(adjusted)}")


def main():
    env = os.getenv("BOT_ENV", "test")
    run_once(env)


if __name__ == "__main__":
    main()
