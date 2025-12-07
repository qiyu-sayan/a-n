# bot/main.py
"""
交易机器人主入口 (OKX + WeCom)

当前设计（适配你的使用方式）：

- 环境：
    Env.TEST  -> OKX 模拟盘（DEMO）
    Env.LIVE  -> OKX 实盘

- DEMO 模拟盘：
    * 无法查询余额 / 持仓 -> 启用“伪余额模式”（假定余额充足）
    * 不做自动止盈止损（OKX 限制持仓接口）
    * 只做：根据策略开仓 + 记录日志 + 发企业微信

- LIVE 实盘：
    * 必须能查余额 / 持仓
    * 上来先跑一遍安全检查（sanity check），失败则本次不交易
    * 使用真实余额做仓位风控
    * 可以开启自动止盈/止损（根据开仓方向 + 盈亏）

- 风控：
    * 每轮最多执行 MAX_ORDERS_PER_RUN 个订单
    * DEMO：固定单笔名义金额上限 (MAX_*_NOTIONAL_TEST)
    * LIVE：单笔名义金额 = min(绝对上限, 总余额的一定比例)
    * 合约名义金额 = amount * last_price （按你手动开 10U 的方式来算）
      -> 我们只按「币数量 * 价格」当作 U 金额，不再折腾 contractSize
"""

from __future__ import annotations

import csv
import os
from datetime import datetime
from typing import List, Tuple, Dict, Optional

import ccxt

from bot.trader import Trader, Env, MarketType, Side, OrderRequest
from bot.wecom_notify import send_wecom_message
from bot import strategy


# =============================
# 全局参数：风控相关
# =============================

# 每次 run-bot 最多执行多少个订单
MAX_ORDERS_PER_RUN = 6

# DEMO：单笔名义金额上限 (USDT)
MAX_SPOT_NOTIONAL_TEST = 10.0
MAX_FUTURES_NOTIONAL_TEST = 10.0

# LIVE：绝对上限 (USDT)
MAX_SPOT_NOTIONAL_LIVE = 10.0
MAX_FUTURES_NOTIONAL_LIVE = 10.0

# LIVE：单笔占总余额的最大比例（例如 0.02 = 2%）
LIVE_RISK_PER_TRADE = 0.02

# LIVE：可用余额低于此值，不再开新仓
MIN_USDT_FREE_FOR_OPEN = 20.0

# 交易日志路径
TRADE_LOG_PATH = os.path.join(
    os.path.dirname(__file__),
    "..",
    "logs",
    "trades.csv",
)


# =============================
# 环境 / Trader / OKX 客户端
# =============================

def _load_env() -> Env:
    env_str = os.getenv("BOT_ENV", "test").lower()
    if env_str == "live":
        return Env.LIVE
    return Env.TEST


def _build_trader() -> Trader:
    paper_keys = {
        "apiKey": os.getenv("OKX_PAPER_API_KEY"),
        "secret": os.getenv("OKX_PAPER_API_SECRET"),
        "password": os.getenv("OKX_PAPER_API_PASSPHRASE"),
    }
    live_keys = {
        "apiKey": os.getenv("OKX_LIVE_API_KEY"),
        "secret": os.getenv("OKX_LIVE_API_SECRET"),
        "password": os.getenv("OKX_LIVE_API_PASSPHRASE"),
    }
    return Trader(
        exchange_id="okx",
        paper_keys=paper_keys,
        live_keys=live_keys,
    )


def _build_okx_clients(env: Env) -> Tuple[ccxt.Exchange, ccxt.Exchange]:
    """
    分别创建 spot / futures 客户端。
    DEMO 环境加上 x-simulated-trading: 1。
    """
    if env == Env.TEST:
        api_key = os.getenv("OKX_PAPER_API_KEY")
        secret = os.getenv("OKX_PAPER_API_SECRET")
        password = os.getenv("OKX_PAPER_API_PASSPHRASE")
        cfg = {
            "apiKey": api_key,
            "secret": secret,
            "password": password,
            "headers": {"x-simulated-trading": "1"},
        }
    else:
        api_key = os.getenv("OKX_LIVE_API_KEY")
        secret = os.getenv("OKX_LIVE_API_SECRET")
        password = os.getenv("OKX_LIVE_API_PASSPHRASE")
        cfg = {
            "apiKey": api_key,
            "secret": secret,
            "password": password,
        }

    spot = ccxt.okx(cfg)
    spot.options["defaultType"] = "spot"

    futures = ccxt.okx(cfg)
    futures.options["defaultType"] = "swap"

    try:
        futures.load_markets()
    except Exception as e:
        print(f"[main] 加载合约市场信息失败: {e}")

    return spot, futures


def _is_demo_unavailable(err: Exception) -> bool:
    msg = str(err)
    return "50038" in msg and "unavailable in demo trading" in msg


# =============================
# 余额 & 仓位相关（DEMO 有特殊处理）
# =============================

def _fetch_usdt_balance(env: Env, spot_ex: ccxt.Exchange) -> Tuple[float, float]:
    """
    返回 (total, free) USDT 余额。
    - DEMO：接口报 50038 -> 启用伪余额模式 (余额很大)
    - LIVE：错误 -> 返回 0，后续风控会阻止开新仓
    """
    try:
        bal = spot_ex.fetch_balance()
        usdt = bal.get("USDT") or {}
        total = float(usdt.get("total") or 0.0)
        free = float(usdt.get("free") or 0.0)
        return total, free
    except Exception as e:
        print(f"[main] 获取余额失败: {e}")
        if env == Env.TEST and _is_demo_unavailable(e):
            print(
                "[notice] 当前为 DEMO 模拟盘，余额接口不可用，"
                "启用『伪余额模式』：假定余额非常充足，仅用于调试策略。"
            )
            return 9_999_999.0, 9_999_999.0
        return 0.0, 0.0


def _get_notional_limits(env: Env, total_usdt: float) -> Tuple[float, float]:
    """
    根据环境返回 (max_spot_notional, max_futures_notional)
    """
    if env == Env.TEST:
        return MAX_SPOT_NOTIONAL_TEST, MAX_FUTURES_NOTIONAL_TEST

    total_usdt = max(0.0, float(total_usdt))
    if total_usdt <= 0:
        return 0.0, 0.0

    dyn_cap = total_usdt * LIVE_RISK_PER_TRADE
    max_spot = min(MAX_SPOT_NOTIONAL_LIVE, dyn_cap)
    max_fut = min(MAX_FUTURES_NOTIONAL_LIVE, dyn_cap)
    return max_spot, max_fut


# =============================
# TP/SL 相关（只给 LIVE 用）
# =============================

def _get_tp_sl_for_leverage(leverage: float) -> Tuple[float, float, float]:
    """
    返回 (tp1, tp2, sl) 的收益率（+0.05 = +5%）
    简单分段：
        lev <= 5  : tp1=5%,  tp2=10%, sl=-3%
        5 < lev<=10: tp1=4%, tp2=8%,  sl=-2%
        >10       : tp1=3%, tp2=6%,  sl=-1.5%
    """
    lev = max(1.0, float(leverage))
    if lev <= 5:
        return 0.05, 0.10, -0.03
    if lev <= 10:
        return 0.04, 0.08, -0.02
    return 0.03, 0.06, -0.015


def _generate_futures_close_orders(
    env: Env,
    fut_ex: ccxt.Exchange,
) -> List[OrderRequest]:
    """
    LIVE：根据当前持仓 + TP/SL 规则生成平仓订单（reduce_only=True）
    DEMO：OKX 不支持持仓查询 -> 直接返回空列表
    """
    close_orders: List[OrderRequest] = []

    try:
        positions = fut_ex.fetch_positions(params={"instType": "SWAP"})
    except Exception as e:
        print(f"[main] 获取合约持仓失败: {e}")
        if env == Env.TEST and _is_demo_unavailable(e):
            print("[notice] DEMO 模拟盘不支持持仓查询，跳过自动止盈止损。")
            return []
        return []

    if not positions:
        return []

    for pos in positions:
        try:
            symbol = pos.get("symbol") or pos.get("info", {}).get("instId")
            if not symbol:
                continue

            size = pos.get("contracts") or pos.get("positionAmt") or pos.get("size")
            if size is None:
                continue
            size = float(size)
            if size == 0:
                continue

            side_raw = (pos.get("side") or pos.get("posSide") or "").lower()
            if side_raw in ("long", "buy"):
                is_long = True
            elif side_raw in ("short", "sell"):
                is_long = False
            else:
                continue

            entry_price = pos.get("entryPrice") or pos.get("avgPx") or pos.get(
                "info", {}
            ).get("avgPx")
            if not entry_price:
                continue
            entry_price = float(entry_price)

            ticker = fut_ex.fetch_ticker(symbol)
            last_price = float(ticker["last"])
            if entry_price <= 0 or last_price <= 0:
                continue

            lev_raw = (
                pos.get("leverage")
                or pos.get("lever")
                or pos.get("info", {}).get("lever")
                or 3
            )
            try:
                lev = float(lev_raw)
            except Exception:
                lev = 3.0

            tp1, tp2, sl = _get_tp_sl_for_leverage(lev)

            if is_long:
                pnl_pct = (last_price - entry_price) / entry_price
            else:
                pnl_pct = (entry_price - last_price) / entry_price

            reason: Optional[str] = None
            side: Optional[str] = None
            amount = 0.0

            if pnl_pct <= sl:
                reason = f"合约止损 lev={lev:.1f}x pnl={pnl_pct*100:.2f}%"
                side = "sell" if is_long else "buy"
                amount = abs(size)
            elif pnl_pct >= tp2:
                reason = f"合约止盈2 lev={lev:.1f}x pnl={pnl_pct*100:.2f}%"
                side = "sell" if is_long else "buy"
                amount = abs(size)
            elif pnl_pct >= tp1:
                reason = f"合约止盈1 lev={lev:.1f}x pnl={pnl_pct*100:.2f}%"
                side = "sell" if is_long else "buy"
                amount = abs(size) * 0.5

            if not reason or not side or amount <= 0:
                continue

            order_side = Side.SELL if side == "sell" else Side.BUY

            close_orders.append(
                OrderRequest(
                    env=env,
                    market=MarketType.FUTURES,
                    symbol=symbol,
                    side=order_side,
                    amount=amount,
                    price=None,
                    leverage=None,
                    position_side=None,
                    reduce_only=True,
                    reason=reason,
                )
            )
        except Exception as e:
            print(f"[main] 处理止盈止损时出错: {e}")
            continue

    return close_orders


# =============================
# 风控：控制名义金额 / 单数
# =============================

def _apply_risk_controls(
    env: Env,
    orders: List[OrderRequest],
    spot_ex: ccxt.Exchange,
    fut_ex: ccxt.Exchange,
    max_spot_notional: float,
    max_fut_notional: float,
) -> List[OrderRequest]:
    """
    - 限制总单数
    - 现货 / 合约统一按：notional = amount * last_price 估算名义金额
    - 超出上限则缩小 amount
    """
    if not orders:
        return []

    # 限制总单数
    if len(orders) > MAX_ORDERS_PER_RUN:
        orders = orders[:MAX_ORDERS_PER_RUN]

    adjusted: List[OrderRequest] = []

    for o in orders:
        try:
            if o.market == MarketType.SPOT:
                ticker = spot_ex.fetch_ticker(o.symbol)
                last = float(ticker["last"])
                if last <= 0:
                    adjusted.append(o)
                    continue

                notional = o.amount * last
                if max_spot_notional > 0 and notional > max_spot_notional:
                    max_amt = max_spot_notional / last
                    print(
                        f"[risk] 现货 {o.symbol} 名义金额 {notional:.2f} 超出限制 "
                        f"{max_spot_notional:.2f}，调整数量为 {max_amt:.6f}"
                    )
                    o.amount = max_amt

            else:  # FUTURES
                ticker = fut_ex.fetch_ticker(o.symbol)
                last = float(ticker["last"])
                if last <= 0:
                    adjusted.append(o)
                    continue

                # 按「币的数量 * 价格」来估算 U 金额
                notional = o.amount * last
                if max_fut_notional > 0 and notional > max_fut_notional:
                    max_amt = max_fut_notional / last
                    print(
                        f"[risk] 合约 {o.symbol} 名义金额 {notional:.2f} 超出限制 "
                        f"{max_fut_notional:.2f}，调整数量为 {max_amt:.6f}"
                    )
                    o.amount = max_amt

        except Exception as e:
            print(f"[risk] 风控处理 {o.symbol} 时出错: {e}")

        adjusted.append(o)

    return adjusted


# =============================
# 金额估算 & WeCom 文本 & 日志
# =============================

def _estimate_notional(
    req: OrderRequest,
    spot_ex: ccxt.Exchange,
    fut_ex: ccxt.Exchange,
) -> float:
    try:
        if req.market == MarketType.SPOT:
            ticker = spot_ex.fetch_ticker(req.symbol)
        else:
            ticker = fut_ex.fetch_ticker(req.symbol)
        last = float(ticker["last"])
        return req.amount * last if last > 0 else 0.0
    except Exception as e:
        print(f"[main] 估算金额失败 {req.symbol}: {e}")
        return 0.0


def _format_wecom_message(
    req: OrderRequest,
    success: bool,
    error: Optional[str],
    notional: float,
) -> str:
    env_tag = "DEMO" if req.env == Env.TEST else "LIVE"
    market_tag = "现货" if req.market == MarketType.SPOT else "合约"
    status = "✅ 成功" if success else "❌ 失败"

    lines = [
        f"[{env_tag}] [{market_tag}] {status}",
        f"品种: {req.symbol}",
        f"方向: {req.side.value}",
    ]

    # 同时显示数量 + 预估金额
    lines.append(f"数量: {req.amount}")
    if notional > 0:
        lines.append(f"预估金额: ~{notional:.2f} USDT")
    else:
        lines.append("预估金额: 估算失败")

    if req.leverage is not None and req.market == MarketType.FUTURES:
        lines.append(f"杠杆: {req.leverage}x")

    if req.reason:
        lines.append(f"原因: {req.reason}")

    if not success and error:
        lines.append(f"错误: {error}")

    return "\n".join(lines)


def _send_batch_wecom_message(env: Env, messages: List[str]) -> None:
    if not messages:
        return
    header_env = "DEMO（OKX 模拟盘）" if env == Env.TEST else "LIVE（OKX 实盘）"
    text = "### 交易机器人执行结果 - " + header_env + "\n\n" + "\n\n".join(messages)
    send_wecom_message(text)


def _append_trade_log(
    env: Env,
    req: OrderRequest,
    success: bool,
    error: Optional[str],
    notional: float,
) -> None:
    try:
        os.makedirs(os.path.dirname(TRADE_LOG_PATH), exist_ok=True)
        is_new = not os.path.exists(TRADE_LOG_PATH)
        with open(TRADE_LOG_PATH, "a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            if is_new:
                writer.writerow(
                    [
                        "timestamp",
                        "env",
                        "market",
                        "symbol",
                        "side",
                        "amount",
                        "notional",
                        "leverage",
                        "reduce_only",
                        "success",
                        "error",
                    ]
                )
            writer.writerow(
                [
                    datetime.utcnow().isoformat(),
                    env.value,
                    req.market.value,
                    req.symbol,
                    req.side.value,
                    f"{req.amount:.8f}",
                    f"{notional:.8f}",
                    req.leverage if req.leverage is not None else "",
                    int(getattr(req, "reduce_only", False)),
                    int(success),
                    (error or "").replace("\n", " ")[:200],
                ]
            )
    except Exception as e:
        print(f"[log] 写入交易日志失败: {e}")


# =============================
# LIVE 环境安全检查
# =============================

def _run_live_sanity_checks(spot_ex: ccxt.Exchange, fut_ex: ccxt.Exchange) -> bool:
    print("[sanity] 正在执行实盘安全检查...")
    try:
        bal = spot_ex.fetch_balance()
        usdt = bal.get("USDT") or {}
        total = float(usdt.get("total") or 0.0)
        free = float(usdt.get("free") or 0.0)
        print(f"[sanity] USDT 余额: total={total:.2f}, free={free:.2f}")
    except Exception as e:
        print(f"[sanity] 余额检查失败: {e}")
        return False

    try:
        markets = fut_ex.load_markets()
        if "BTC/USDT:USDT" in markets:
            print("[sanity] 市场检查: BTC/USDT:USDT 可用。")
        else:
            print("[sanity] 市场检查: 未找到 BTC/USDT:USDT。")
    except Exception as e:
        print(f"[sanity] 市场检查失败: {e}")
        return False

    print("[sanity] 实盘安全检查通过。")
    return True


# =============================
# 主流程
# =============================

def run_once() -> None:
    env = _load_env()
    trader = _build_trader()
    spot_ex, fut_ex = _build_okx_clients(env)

    print(f"[main] 运行环境: {env.value}")
    if env == Env.TEST:
        print(
            "[notice] 当前为 DEMO 模拟盘环境，"
            "用于调试策略与风控逻辑，请勿视为真实盈亏。"
        )

    if env == Env.LIVE:
        if not _run_live_sanity_checks(spot_ex, fut_ex):
            print("[main] 实盘安全检查未通过，本次不交易。")
            return

    # 余额 & 名义金额上限
    total_usdt, free_usdt = _fetch_usdt_balance(env, spot_ex)
    print(f"[main] 账户 USDT 余额: total={total_usdt:.2f}, free={free_usdt:.2f}")
    max_spot_notional, max_fut_notional = _get_notional_limits(env, total_usdt)
    print(
        f"[main] 单笔名义金额上限: 现货≈{max_spot_notional:.2f}U, "
        f"合约≈{max_fut_notional:.2f}U"
    )

    # LIVE：余额太低时，不开新仓（只平仓）
    can_open_new = True
    if env == Env.LIVE and free_usdt < MIN_USDT_FREE_FOR_OPEN:
        print(
            f"[main] LIVE 可用余额 {free_usdt:.2f} 低于阈值 "
            f"{MIN_USDT_FREE_FOR_OPEN:.2f}，本轮仅允许平仓。"
        )
        can_open_new = False

    # 止盈止损（只对合约、且 DEMO 基本会被跳过）
    close_orders = _generate_futures_close_orders(env, fut_ex)
    if close_orders:
        print(f"[main] 仓位管理生成 {len(close_orders)} 个平仓订单。")

    # 策略开仓
    open_orders: List[OrderRequest] = []
    if can_open_new:
        open_orders = strategy.generate_orders(env)

    if not close_orders and not open_orders:
        print("[main] 本次无交易信号，退出。")
        return

    all_orders = close_orders + open_orders
    print(f"[main] 原始订单数量: {len(all_orders)}")

    # 风控
    final_orders = _apply_risk_controls(
        env,
        all_orders,
        spot_ex,
        fut_ex,
        max_spot_notional,
        max_fut_notional,
    )

    print(f"[main] 风控后实际执行 {len(final_orders)} 个订单。")

    if not final_orders:
        print("[main] 风控过滤掉所有订单，本次不交易。")
        return

    # 下单 + 推送 + 日志
    messages: List[str] = []

    for idx, req in enumerate(final_orders, start=1):
        print(
            f"[main] ({idx}/{len(final_orders)}) 下单: "
            f"{req.market.value} {req.symbol} {req.side.value} amount={req.amount}"
        )

        res = trader.place_order(req)
        notional = _estimate_notional(req, spot_ex, fut_ex)
        msg = _format_wecom_message(req, res.success, res.error, notional)
        print(msg)

        messages.append(msg)
        _append_trade_log(env, req, res.success, res.error, notional)

    try:
        _send_batch_wecom_message(env, messages)
        print("[main] 企业微信推送已发送。")
    except Exception as e:
        print(f"[main] 企业微信推送失败: {e}")


if __name__ == "__main__":
    run_once()
