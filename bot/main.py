# bot/main.py
from __future__ import annotations

import csv
import os
from datetime import datetime
from typing import List, Tuple, Optional

import ccxt

from bot.strategy import generate_orders
from bot.wecom_notify import send_wecom_message  # 你的 wecom_notify 里是 send_wecom_message
from bot.trader import (
    Trader,
    OrderRequest,
    MarketType,
    PositionSide,
)
from bot.virtual_pnl import VirtualPositionManager, ClosedTrade


# =============================
# 环境定义
# =============================

class Env:
    TEST = "test"
    LIVE = "live"


def now_ts() -> str:
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")


# =============================
# 名义金额估算
# =============================

def _estimate_notional(ex: ccxt.Exchange, market: MarketType, symbol: str, amount: float) -> float:
    """
    尝试预估名义金额 = amount * 最新价格。
    """
    try:
        ticker = ex.fetch_ticker(symbol)
        price: Optional[float] = (
            ticker.get("last")
            or ticker.get("close")
            or ticker.get("ask")
            or ticker.get("bid")
        )

        if price is None:
            m = ex.market(symbol)
            info = m.get("info", {})
            price = info.get("last") or info.get("idxPx") or info.get("markPx")

        if price is None:
            return 0.0

        return abs(float(amount)) * float(price)

    except Exception:
        return 0.0


# =============================
# 风控参数
# =============================

ORDER_USDT = float(os.getenv("ORDER_USDT", "10"))  # 策略里也会用到

MAX_ORDERS_PER_RUN = 3
MAX_FUTURES_NOTIONAL_LIVE = 50_000
LIVE_RISK_PER_TRADE = 0.05  # 5%

MAX_FUTURES_NOTIONAL_TEST = ORDER_USDT  # DEMO 时只是用来估算和打印


# =============================
# 创建 OKX 客户端
# =============================

def create_okx_exchanges(env: str) -> Tuple[ccxt.Exchange, ccxt.Exchange]:
    if env == Env.TEST:
        api_key = os.getenv("OKX_PAPER_API_KEY")
        api_secret = os.getenv("OKX_PAPER_API_SECRET")
        api_passphrase = os.getenv("OKX_PAPER_API_PASSPHRASE")
    else:
        api_key = os.getenv("OKX_LIVE_API_KEY")
        api_secret = os.getenv("OKX_LIVE_API_SECRET")
        api_passphrase = os.getenv("OKX_LIVE_API_PASSPHRASE")

    base_config = {
        "apiKey": api_key,
        "secret": api_secret,
        "password": api_passphrase,
        "enableRateLimit": True,
    }

    spot_ex = ccxt.okx({**base_config, "options": {"defaultType": "spot"}})
    fut_ex = ccxt.okx({**base_config, "options": {"defaultType": "swap", "defaultSettle": "usdt"}})

    # 测试环境打开模拟盘
    if env == Env.TEST:
        spot_ex.set_sandbox_mode(True)
        fut_ex.set_sandbox_mode(True)

    return spot_ex, fut_ex


# =============================
# 风控逻辑
# =============================

def _apply_risk_controls(
    env: str,
    spot_ex: ccxt.Exchange,
    fut_ex: ccxt.Exchange,
    orders: List[OrderRequest],
) -> List[Tuple[OrderRequest, float]]:
    """
    返回：[(订单, 预估名义金额)]

    TEST（模拟盘）：
        - 不缩小数量，只估算名义金额供观测使用
    LIVE（实盘）：
        - 按余额 + 风险比例裁剪
    """
    adjusted: List[Tuple[OrderRequest, float]] = []

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
        notional = _estimate_notional(ex, req.market, req.symbol, req.amount)

        # TEST：只估算，不缩量
        if env == Env.TEST:
            if notional <= 0:
                notional = MAX_FUTURES_NOTIONAL_TEST
                print(f"[risk] DEMO 无法预估名义金额，使用默认 ORDER_USDT={notional}。")
            else:
                print(
                    f"[risk] DEMO 预估名义金额: symbol={req.symbol}, "
                    f"amount={req.amount:.6f}, notional≈{notional:.4f} USDT"
                )
            adjusted.append((req, notional))
            count += 1
            continue

        # LIVE：严格限制
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
    path = f"logs/trades_{env}.csv"
    exists = os.path.exists(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)

    with open(path, "a", newline="", encoding="utf8") as f:
        w = csv.writer(f)
        if not exists:
            w.writerow(
                [
                    "ts",
                    "symbol",
                    "side",
                    "position_side",
                    "amount",
                    "notional",
                    "lev",
                    "order_id",
                    "reason",
                ]
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
                (req.reason or "").replace("\n", " | "),
            ]
        )


def _get_fill_price(ex: ccxt.Exchange, symbol: str) -> Optional[float]:
    """尽量获取当前成交价，用于虚拟开平仓。"""
    try:
        t = ex.fetch_ticker(symbol)
        price = (
            t.get("last")
            or t.get("close")
            or t.get("ask")
            or t.get("bid")
        )
        if price:
            return float(price)
    except Exception:
        pass
    return None


# =============================
# 主流程
# =============================

def run_once(env: str):
    print(f"[main] 运行环境: {env}")
    if env == Env.TEST:
        print("Notice: 当前为 DEMO 模拟盘环境，用于调试策略与风控逻辑，请勿视为实盘。")

    spot_ex, fut_ex = create_okx_exchanges(env)
    trader = Trader(env, spot_ex, fut_ex)
    vp_manager = VirtualPositionManager(env)

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

    wecom_msgs: List[str] = []
    success_count = 0
    closed_trades: List[ClosedTrade] = []

    for req, notional in adjusted:
        print(
            f"[main] 下单请求: {req.symbol} {req.side.value} "
            f"amount={req.amount}, lev={req.leverage}"
        )

        ok, result = trader.place_order(req)

        # 获取用于虚拟开平仓的“成交价”
        fill_price = _get_fill_price(fut_ex if req.market == MarketType.FUTURES else spot_ex)
        closed: Optional[ClosedTrade] = None
        if ok and fill_price is not None and req.market == MarketType.FUTURES:
            closed = vp_manager.on_order_filled(req, fill_price)
            if closed:
                closed_trades.append(closed)

        if ok:
            success_count += 1
            order_id = result.get("order_id", "")
            msg = (
                f"【成功】{req.symbol} {req.side.value} x{req.leverage}\n"
                f"数量: {req.amount}, notional≈{notional:.2f}\n"
                f"信号:\n{req.reason}"
            )
            if closed:
                pnl_flag = "✅" if closed.pnl > 0 else "❌"
                msg += (
                    f"\n\n【虚拟平仓】方向: {closed.side.value}, 数量: {closed.qty}\n"
                    f"开仓价: {closed.entry_price:.4f}, 平仓价: {closed.exit_price:.4f}\n"
                    f"PNL≈{closed.pnl:.4f} USDT {pnl_flag}"
                )
            wecom_msgs.append(msg)
            _log_trade(req, result, env, notional)
        else:
            msg = (
                f"【失败】{req.symbol} {req.side.value}\n"
                f"错误: {result.get('error')}\n"
                f"信号:\n{req.reason}"
            )
            wecom_msgs.append(msg)

        print("[main] 下单结果:", ok, result)

    # 汇总本轮虚拟平仓情况
    if closed_trades:
        wins = sum(1 for t in closed_trades if t.pnl > 0)
        total = len(closed_trades)
        total_pnl = sum(t.pnl for t in closed_trades)
        summary = (
            f"本轮虚拟平仓 {total} 笔，胜率 {wins}/{total}"
            f" ({wins / total:.2%})，合计 PNL≈{total_pnl:.4f} USDT"
        )
        wecom_msgs.append(summary)
        print("[virtual] " + summary)

    # WeCom 推送（用的是 wecom_notify 里的 send_wecom_message）
    if wecom_msgs:
        text = "【交易机器人执行结果 - DEMO (OKX 模拟盘)】\n\n" + "\n\n".join(wecom_msgs)
        send_wecom_message(text)

    print(f"[main] 本轮完成：成功 {success_count} / {len(adjusted)}")


def main():
    env = os.getenv("BOT_ENV", Env.TEST)
    run_once(env)


if __name__ == "__main__":
    main()
