"""
交易机器人主入口 (OKX + WeCom)

当前设计（适配你的使用方式）：

- 环境：
    Env.TEST  -> OKX 模拟盘 (DEMO)
    Env.LIVE  -> OKX 实盘

- DEMO 模拟盘：
    * 无法查询余额 / 持仓 -> 启用“伪余额模式”（假设余额充足）
    * 不做自动止盈止损（OKX 限制持仓接口）
    * 只做：根据策略开仓 + 记录日志 + 发企业微信

- LIVE 实盘：
    * 必须能查余额 / 持仓
    * 上来先跑一遍安全检查（sanity check），失败则本次不交易
    * 使用真实余额做仓位风控
    * 可以开启自动止盈/止损（根据持仓方向 + 盈亏）

- 风控：
    * 每轮最多执行 MAX_ORDERS_PER_RUN 个订单
    * DEMO: 固定单笔名义金额上限 (MAX_*_NOTIONAL_TEST)
    * LIVE: 单笔名义金额上限 (MAX_*_NOTIONAL_LIVE) + 按总余额比例控制 (LIVE_RISK_PER_TRADE)
    * 名义金额 = amount * last_price（按你平时开 100U 的方式来算）
"""

from __future__ import annotations

import csv
import os
from datetime import datetime, timedelta
from collections import Counter
from typing import List, Tuple, Dict, Optional

import ccxt

from bot.trader import (
    Trader,
    Env,
    MarketType,
    Side,
    PositionSide,
    OrderRequest,
)
from bot.strategy import generate_orders
from bot.wecom_notify import send_wecom_message


# =============================
# 常量 & 配置
# =============================

# 每次 run-bot 最多执行多少个订单
MAX_ORDERS_PER_RUN = 6

# 从环境变量读取单笔目标名义金额 (USDT)，方便你在 GitHub Secrets 里调整
# 例如：ORDER_USDT = "10" -> 单笔名义金额大约 10U
ORDER_USDT = float(os.getenv("ORDER_USDT", "10"))

# DEMO：单笔名义金额上限 (USDT)
MAX_SPOT_NOTIONAL_TEST = ORDER_USDT
MAX_FUTURES_NOTIONAL_TEST = ORDER_USDT

# LIVE：绝对上限 (USDT)
MAX_SPOT_NOTIONAL_LIVE = ORDER_USDT
MAX_FUTURES_NOTIONAL_LIVE = ORDER_USDT

# LIVE：单笔占总余额的最大比例（例如 0.02 = 2%）
LIVE_RISK_PER_TRADE = 0.02

# TP / SL（只在 LIVE，用持仓 + 未实现盈亏判断）
TAKE_PROFIT_PCT = float(os.getenv("TAKE_PROFIT_PCT", "1.5"))  # 盈利超过 1.5% 触发止盈
STOP_LOSS_PCT = float(os.getenv("STOP_LOSS_PCT", "1.0"))     # 亏损超过 1.0% 触发止损

# 交易日志路径（GitHub Actions Runner 本地）
TRADE_LOG_PATH = "logs/trades.csv"


# =============================
# 工具函数：OKX 账号 / 行情 / 名义金额
# =============================

def _load_okx_env(env: Env) -> Tuple[str, str, str]:
    """
    返回 (api_key, api_secret, passphrase)
    """
    if env == Env.TEST:
        key = os.environ["OKX_PAPER_API_KEY"]
        secret = os.environ["OKX_PAPER_API_SECRET"]
        passphrase = os.environ["OKX_PAPER_API_PASSPHRASE"]
    else:
        key = os.environ["OKX_LIVE_API_KEY"]
        secret = os.environ["OKX_LIVE_API_SECRET"]
        passphrase = os.environ["OKX_LIVE_API_PASSPHRASE"]
    return key, secret, passphrase


def _create_exchanges(env: Env) -> Tuple[ccxt.Exchange, ccxt.Exchange]:
    api_key, api_secret, passphrase = _load_okx_env(env)

    # 现货
    spot = ccxt.okx({
        "apiKey": api_key,
        "secret": api_secret,
        "password": passphrase,
    })
    # 永续合约
    futures = ccxt.okx({
        "apiKey": api_key,
        "secret": api_secret,
        "password": passphrase,
    })

    # 某些 ccxt 版本 OKX id 不统一，这里强制为 okx
    try:
        spot.id = "okx"
    except Exception:
        pass
    try:
        futures.id = "okx"
    except Exception:
        pass

    if env == Env.TEST:
        try:
            spot.set_sandbox_mode(True)
            futures.set_sandbox_mode(True)
        except Exception as e:
            print(f"[main] 启用 sandbox 模式失败: okx {e}")

    return spot, futures


def _load_markets(spot: ccxt.Exchange, futures: ccxt.Exchange) -> None:
    # 只是尝试一次，如果 DEMO 报 50038 就忽略
    try:
        spot.load_markets()
    except Exception as e:
        print(f"[main] 加载现货市场信息失败: okx {e}")
    try:
        futures.load_markets()
    except Exception as e:
        print(f"[main] 加载合约市场信息失败: okx {e}")


def _estimate_notional(
    ex: ccxt.Exchange,
    market: MarketType,
    symbol: str,
    amount: float,
) -> float:
    try:
        m = ex.market(symbol)
        info = m.get("info", {})
        # 价格优先用 last / idxPx，如果没有就退而求其次
        price = float(info.get("last") or info.get("idxPx") or 0.0)
        return abs(amount) * price
    except Exception as e:
        print(f"[main] 预估名义金额失败 ({symbol}): {e}")
        return 0.0


# =============================
# 自动止盈 / 止损（仅 LIVE）
# =============================

def _generate_futures_close_orders(
    env: Env,
    fut_ex: ccxt.Exchange,
) -> List[OrderRequest]:
    """
    LIVE：根据当前持仓 + TP/SL 规则生成平仓订单（reduce_only=True）
    DEMO：不做自动止盈止损（OKX 模拟盘不支持持仓查询）
    """
    close_orders: List[OrderRequest] = []

    # 只在 LIVE 实盘启用自动止盈/止损
    if env != Env.LIVE:
        return []

    try:
        positions = fut_ex.fetch_positions(params={"instType": "SWAP"})
    except Exception as e:
        print(f"[main] 获取合约持仓失败: okx {e}")
        return []

    now = datetime.utcnow().isoformat(timespec="seconds")

    for pos in positions:
        try:
            info = pos.get("info", {})
            symbol = pos.get("symbol") or info.get("instId")
            if not symbol:
                continue

            side_str = pos.get("side") or info.get("posSide")
            if not side_str:
                continue

            side_str = str(side_str).lower()
            if side_str == "long":
                side = Side.SELL
                pos_side = PositionSide.LONG
            elif side_str == "short":
                side = Side.BUY
                pos_side = PositionSide.SHORT
            else:
                continue

            # 这里直接用名义价值（或仓位张数 * 标记价），不同账户略有差异
            notional = float(pos.get("notional", 0) or pos.get("contracts", 0))
            if notional <= 0:
                continue

            upnl = float(pos.get("unrealizedPnl", 0))
            upnl_pct = (upnl / notional) * 100 if notional > 0 else 0.0

            # 止盈
            if upnl_pct >= TAKE_PROFIT_PCT:
                reason = (
                    f"自动止盈: upnl={upnl:.4f}({upnl_pct:.2f}%) >= {TAKE_PROFIT_PCT:.2f}%，"
                    f"time={now}"
                )
            # 止损
            elif upnl_pct <= -STOP_LOSS_PCT:
                reason = (
                    f"自动止损: upnl={upnl:.4f}({upnl_pct:.2f}%) <= -{STOP_LOSS_PCT:.2f}%，"
                    f"time={now}"
                )
            else:
                continue

            amount = abs(float(pos.get("contracts", 0) or info.get("pos", 0)))
            if amount <= 0:
                continue

            close_orders.append(
                OrderRequest(
                    env=env,
                    market=MarketType.FUTURES,
                    symbol=symbol,
                    side=side,
                    position_side=pos_side,
                    amount=amount,
                    leverage=None,
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
    spot_ex: ccxt.Exchange,
    fut_ex: ccxt.Exchange,
    orders: List[OrderRequest],
) -> List[Tuple[OrderRequest, float]]:
    """
    返回：[(订单, 预估名义金额), ...]，已经按名义金额和最大单数做了裁剪。
    """
    adjusted: List[Tuple[OrderRequest, float]] = []

    # DEMO: 假装余额很大，只按固定上限裁剪
    demo_spot_cap = MAX_SPOT_NOTIONAL_TEST
    demo_fut_cap = MAX_FUTURES_NOTIONAL_TEST

    # LIVE: 尝试获取真实余额
    live_total_usdt = 0.0
    live_free_usdt = 0.0
    if env == Env.LIVE:
        try:
            balance = spot_ex.fetch_balance(params={"type": "trade"})
            usdt = balance.get("USDT") or balance.get("usdt") or {}
            live_total_usdt = float(usdt.get("total") or 0.0)
            live_free_usdt = float(usdt.get("free") or 0.0)
            print(f"[main] 账户 USDT 余额: total={live_total_usdt:.2f}, free={live_free_usdt:.2f}")
        except Exception as e:
            print(f"[main] 获取余额失败: okx {e}")
            # 安全起见，失败就直接不下单
            return []

    # 统计已经加入的订单数，控制 MAX_ORDERS_PER_RUN
    order_count = 0

    for req in orders:
        if order_count >= MAX_ORDERS_PER_RUN:
            print("[risk] 当轮订单数已达上限，剩余信号忽略。")
            break

        ex = spot_ex if req.market == MarketType.SPOT else fut_ex
        notional = _estimate_notional(ex, req.market, req.symbol, req.amount)

        if notional <= 0:
            print(f"[risk] 无法预估名义金额，跳过: {req}")
            continue

        # DEMO 环境：只按固定 U 上限裁剪
        if env == Env.TEST:
            cap = demo_spot_cap if req.market == MarketType.SPOT else demo_fut_cap
            if notional > cap:
                scale = cap / notional
                old_amount = req.amount
                req.amount *= scale
                notional = cap
                print(
                    f"[risk] DEMO 名义金额 {old_amount:.4f} 超出上限，"
                    f"调整为 amount={req.amount:.6f}, notional={notional:.2f}"
                )
        else:
            # LIVE 环境：上限 = min(绝对 U 上限, 余额 * 比例) + 不超过可用余额
            abs_cap = MAX_SPOT_NOTIONAL_LIVE if req.market == MarketType.SPOT else MAX_FUTURES_NOTIONAL_LIVE
            dyn_cap = live_total_usdt * LIVE_RISK_PER_TRADE
            cap = min(abs_cap, dyn_cap)

            if cap <= 0 or live_free_usdt <= 0:
                print("[risk] LIVE 环境余额不足或风险参数为 0，本轮不交易。")
                return []

            cap = min(cap, live_free_usdt)

            if notional > cap:
                scale = cap / notional
                old_amount = req.amount
                req.amount *= scale
                notional = cap
                print(
                    f"[risk] LIVE 名义金额 {old_amount:.4f} 超出上限，"
                    f"调整为 amount={req.amount:.6f}, notional={notional:.2f}"
                )

        adjusted.append((req, notional))
        order_count += 1

    return adjusted


# =============================
# WeCom 推送 & 日志
# =============================

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

    # 推送里只突出 U 金额，数量留在日志里
    if notional > 0:
        lines.append(f"下单金额: ~{notional:.2f} USDT")
    else:
        lines.append("下单金额: 估算失败")

    if req.leverage is not None and req.market == MarketType.FUTURES:
        lines.append(f"杠杆: {req.leverage}x")

    if req.reason:
        lines.append(f"原因: {req.reason}")

    if not success and error:
        lines.append(f"错误: {error}")

    return "\n".join(lines)


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
        with open(TRADE_LOG_PATH, "a", encoding="utf-8", newline="") as f:
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


def _build_recent_report(env: Env, hours: int = 24) -> str:
    """读取 trades.csv，统计最近 hours 小时内的简单战报。"""
    if not os.path.exists(TRADE_LOG_PATH):
        return "暂无历史交易记录。"

    now = datetime.utcnow()
    cutoff = now - timedelta(hours=hours)

    totals = {
        "count": 0,
        "long": 0,
        "short": 0,
    }
    by_market: Counter[str] = Counter()
    by_symbol: Counter[str] = Counter()

    try:
        with open(TRADE_LOG_PATH, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    if row.get("env") != env.value:
                        continue
                    ts_str = row.get("timestamp")
                    if not ts_str:
                        continue
                    ts = datetime.fromisoformat(ts_str)
                    if ts < cutoff:
                        continue

                    totals["count"] += 1

                    side = (row.get("side") or "").lower()
                    if side == "buy":
                        totals["long"] += 1
                    elif side == "sell":
                        totals["short"] += 1

                    market = row.get("market") or "unknown"
                    by_market[market] += 1

                    symbol = row.get("symbol") or "unknown"
                    by_symbol[symbol] += 1
                except Exception:
                    continue
    except Exception as e:
        print(f"[report] 生成战报失败: {e}")
        return "战报生成失败。"

    if totals["count"] == 0:
        return f"最近 {hours} 小时内无交易。"

    lines: List[str] = []
    lines.append(
        f"最近 {hours} 小时共 {totals['count']} 笔订单（多 {totals['long']} / 空 {totals['short']}）。"
    )

    if by_market:
        parts = [f"{k}:{v}" for k, v in by_market.items()]
        lines.append("按市场： " + "； ".join(parts))

    if by_symbol:
        top_symbols = by_symbol.most_common(10)
        parts = [f"{sym}:{cnt}" for sym, cnt in top_symbols]
        lines.append("按币种： " + "； ".join(parts))

    return "\n".join(lines)


def _send_batch_wecom_message(env: Env, messages: List[str]) -> None:
    """把本轮执行结果 + 最近 24 小时战报，一起发到企业微信。"""
    header_env = "DEMO（OKX 模拟盘）" if env == Env.TEST else "LIVE（OKX 实盘）"

    sections: List[str] = []
    sections.append("### 交易机器人执行结果 - " + header_env)

    if messages:
        sections.append("\n".join(messages))

    # 追加 24 小时简易战报
    report = _build_recent_report(env, hours=24)
    sections.append("### 近 24 小时策略战报")
    sections.append(report)

    text = "\n\n".join(sections)
    send_wecom_message(text)


# =============================
# LIVE 环境安全检查
# =============================

def _run_live_sanity_checks(
    spot_ex: ccxt.Exchange,
    fut_ex: ccxt.Exchange,
) -> bool:
    """LIVE 环境安全检查：检查余额是否正常。"""
    try:
        balance = spot_ex.fetch_balance(params={"type": "trade"})
        usdt = balance.get("USDT") or balance.get("usdt") or {}
        total = float(usdt.get("total") or 0.0)
        free = float(usdt.get("free") or 0.0)
        print(f"[sanity] LIVE 余额检查: total={total:.2f}, free={free:.2f}")
        if total <= 0 or free <= 0:
            print("[sanity] 余额不足，安全检查失败。")
            return False
        return True
    except Exception as e:
        print(f"[sanity] LIVE 安全检查失败: {e}")
        return False


# =============================
# 主入口：单次运行
# =============================

def run_once(env: Env) -> None:
    print(f"[main] 运行环境: {env.value}")

    spot_ex, fut_ex = _create_exchanges(env)
    _load_markets(spot_ex, fut_ex)

    trader = Trader(env, spot_ex, fut_ex)

    if env == Env.LIVE:
        if not _run_live_sanity_checks(spot_ex, fut_ex):
            print("[main] LIVE 安全检查未通过，本次不交易。")
            return
    else:
        print("Notice：当前为 DEMO 模拟盘环境，用于调试策略与风控逻辑，请勿视为实盘。")

    # 生成本轮的开仓信号（‼️ 只传 trader）
    open_orders = generate_orders(trader)

    # 生成自动止盈/止损平仓订单（仅 LIVE）
    close_orders = _generate_futures_close_orders(env, fut_ex)

    all_orders = open_orders + close_orders
    if not all_orders:
        print("[main] 本次无交易信号，退出。")
        return

    # 风控裁剪
    adjusted = _apply_risk_controls(env, spot_ex, fut_ex, all_orders)
    if not adjusted:
        print("[main] 风控后无有效订单，本次不下单。")
        return

    print(f"[main] 策略生成 {len(all_orders)} 个原始订单，风控后保留 {len(adjusted)} 个订单。")

    wecom_msgs: List[str] = []

    for req, notional in adjusted:
        success = False
        error_msg = None

        try:
            if req.market == MarketType.SPOT:
                print(
                    f"[main] (SPOT) 下单: {req.symbol} {req.side.value} "
                    f"amount={req.amount:.8f}, notional~{notional:.2f}"
                )
                trader.place_spot_order(req)
            else:
                print(
                    f"[main] (FUT) 下单: {req.symbol} {req.side.value} "
                    f"amount={req.amount:.8f}, notional~{notional:.2f}, "
                    f"lev={req.leverage}x, reduce_only={getattr(req, 'reduce_only', False)}"
                )
                trader.place_futures_order(req)

            success = True
        except Exception as e:
            error_msg = str(e)
            print(f"[main] 下单异常: {e}")

        _append_trade_log(env, req, success, error_msg, notional)
        wecom_msgs.append(_format_wecom_message(req, success, error_msg, notional))

    _send_batch_wecom_message(env, wecom_msgs)


if __name__ == "__main__":
    # 同时兼容 ENV / BOT_ENV 两种环境变量
    env_str = (os.getenv("ENV") or os.getenv("BOT_ENV") or "test").lower()
    env = Env.TEST if env_str == "test" else Env.LIVE
    run_once(env)
