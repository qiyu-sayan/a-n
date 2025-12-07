# bot/main.py
"""
交易机器人主入口（OKX + 企业微信）

功能概览：
1. 根据 BOT_ENV 选择 DEMO / LIVE
2. 初始化 OKX Trader + ccxt 客户端
3. DEMO / LIVE 不同处理：
   - DEMO:
       * 余额/持仓接口不可用 -> 启用“伪余额模式”，只做开仓风控，不做余额限制
       * 自动止盈止损因为拿不到持仓，只能依赖策略层（以后实盘再用）
   - LIVE:
       * 运行实盘安全检查（Sanity Check），失败则不交易
       * 必须获取真实余额，用来控制每单金额
4. 调用策略生成开仓订单（现货 + 合约 + 动态杠杆）
5. 风控：
   - 去重
   - 限制每轮最大订单数
   - 动态控制单笔名义金额（LIVE 按余额的一定比例）
6. 下单并估算金额
7. 写入交易日志 CSV（后续可用于胜率分析 / 收益曲线）
8. 推送企业微信（金额视角，不带订单号）
"""

import os
import csv
from datetime import datetime
from typing import List, Tuple, Dict

import ccxt

from bot.trader import (
    Trader,
    Env,
    MarketType,
    OrderRequest,
    Side,
)
from bot.wecom_notify import send_wecom_message
from bot import strategy

# =============================
# 风控 & 参数
# =============================

# 每次 run-bot 最多执行多少个订单（现货+合约总和）
MAX_ORDERS_PER_RUN = 6

# 同一市场 + 同一 symbol + 同一方向 + reduce_only，每轮只保留一单
DEDUPE_SAME_SYMBOL_SIDE = True

# DEMO：固定名义金额上限（USDT）
MAX_SPOT_NOTIONAL_TEST = 10.0
MAX_FUTURES_NOTIONAL_TEST = 10.0  # 原来 20.0，改成 10.0

# LIVE：绝对上限
MAX_SPOT_NOTIONAL_LIVE = 10.0
MAX_FUTURES_NOTIONAL_LIVE = 10.0


# LIVE：单笔名义金额占总余额的最大比例（例如 0.02 = 2%）
LIVE_RISK_PER_TRADE = 0.02

# 合约单笔最大张数（再保险）
MAX_FUTURES_CONTRACTS_PER_ORDER = 100

# LIVE：余额门槛，低于这个值时，不再开新仓，只允许平仓
MIN_USDT_FREE_FOR_OPEN = 20.0

# 交易日志文件（相对仓库根目录）
TRADE_LOG_PATH = os.path.join(
    os.path.dirname(__file__), "..", "logs", "trades.csv"
)


# =============================
# 环境 & Trader
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


# =============================
# OKX 客户端（给风控 & 持仓管理用）
# =============================

def _build_okx_clients(env: Env) -> Tuple[ccxt.Exchange, ccxt.Exchange]:
    """
    创建带密钥的 OKX ccxt 客户端：
    - TEST 环境：使用 OKX_PAPER_xxx，并加上 x-simulated-trading 头，访问模拟盘
    - LIVE 环境：使用 OKX_LIVE_xxx，访问实盘
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

    if not api_key or not secret or not password:
        print("[main] 警告: OKX API Key/Secret/Passphrase 有缺失，"
              "余额/持仓等私有接口会失败。")

    spot = ccxt.okx(cfg)
    spot.options["defaultType"] = "spot"

    swap = ccxt.okx(cfg)
    swap.options["defaultType"] = "swap"

    try:
        swap.load_markets()
    except Exception as e:
        print(f"[main] 加载合约市场信息失败: {e}")

    return spot, swap


def _is_demo_feature_unavailable(err: Exception) -> bool:
    msg = str(err)
    return "50038" in msg and "unavailable in demo trading" in msg


def _fetch_usdt_balance(env: Env, spot_ex: ccxt.Exchange) -> Tuple[float, float]:
    """
    返回 (total, free) USDT 余额。
    - DEMO: 如果接口不可用，则假定余额充足，同时打印清晰提示
    - LIVE: 接口错误则返回 0，阻止开新仓
    """
    try:
        bal = spot_ex.fetch_balance()
        usdt = bal.get("USDT") or {}
        total = float(usdt.get("total") or 0.0)
        free = float(usdt.get("free") or 0.0)
        return total, free
    except Exception as e:
        print(f"[main] 获取余额失败: {e}")
        if env == Env.TEST and _is_demo_feature_unavailable(e):
            print("[notice] 当前为 DEMO 模拟盘，余额接口不可用，"
                  "启用『伪余额模式』：假定余额非常充足，仅用于调试策略。")
            return 9_999_999.0, 9_999_999.0
        return 0.0, 0.0


def _get_notional_limits(env: Env, total_usdt: float) -> Tuple[float, float]:
    """
    返回 (max_spot_notional, max_futures_notional)

    - DEMO: 使用固定上限（20U），方便统一对比策略效果
    - LIVE: 使用 min(绝对上限, 总余额 * 风险比例)
    """
    if env == Env.TEST:
        return MAX_SPOT_NOTIONAL_TEST, MAX_FUTURES_NOTIONAL_TEST

    # LIVE
    total_usdt = max(0.0, float(total_usdt))
    if total_usdt <= 0:
        # 没余额就算 0，上层会阻止开新仓
        return 0.0, 0.0

    dynamic_cap = total_usdt * LIVE_RISK_PER_TRADE
    max_spot = min(MAX_SPOT_NOTIONAL_LIVE, dynamic_cap)
    max_fut = min(MAX_FUTURES_NOTIONAL_LIVE, dynamic_cap)
    return max_spot, max_fut


# =============================
# 根据杠杆决定 TP/SL（动态）
# =============================

def _get_tp_sl_for_leverage(leverage: float) -> Tuple[float, float, float]:
    """
    返回 (tp1, tp2, sl)：
        tp1: 第一档止盈（平 50%）
        tp2: 第二档止盈（平全部）
        sl:  止损（平全部）
    单位：收益率（+0.05 表示 +5%）
    """
    lev = max(1.0, float(leverage))

    if lev <= 5:
        tp1 = 0.05   # +5%
        tp2 = 0.10   # +10%
        sl = -0.03   # -3%
    elif lev <= 10:
        tp1 = 0.04   # +4%
        tp2 = 0.08   # +8%
        sl = -0.02   # -2%
    else:
        tp1 = 0.03   # +3%
        tp2 = 0.06   # +6%
        sl = -0.015  # -1.5%

    return tp1, tp2, sl


# =============================
# 合约自动止盈 / 止损 -> 生成平仓订单
# =============================

def _generate_futures_close_orders(env: Env, fut_ex: ccxt.Exchange) -> List[OrderRequest]:
    """
    检查合约持仓，如果达到止盈 / 止损条件，生成平仓订单（reduce_only = True）。

    DEMO: 如果接口不可用则跳过自动止盈止损（OKX demo 限制）。
    LIVE: 可正常使用，建议与风控一起配合。
    """
    close_orders: List[OrderRequest] = []

    try:
        positions = fut_ex.fetch_positions(params={"instType": "SWAP"})
    except Exception as e:
        print(f"[main] 获取合约持仓失败: {e}")
        if env == Env.TEST and _is_demo_feature_unavailable(e):
            print("[notice] DEMO 模拟盘不支持持仓查询，跳过自动止盈止损。")
            return []
        return []

    if not positions:
        return []

    for pos in positions:
        try:
            symbol = pos.get("symbol")
            if not symbol:
                info = pos.get("info", {})
                symbol = info.get("instId") or info.get("symbol")
            if not symbol:
                continue

            size = pos.get("contracts") or pos.get("positionAmt") or pos.get("size")
            if size is None:
                continue
            size = float(size)
            if size == 0:
                continue

            side_raw = (pos.get("side") or pos.get("direction") or "").lower()
            if not side_raw and "posSide" in pos:
                side_raw = str(pos["posSide"]).lower()

            if side_raw in ("long", "buy"):
                is_long = True
            elif side_raw in ("short", "sell"):
                is_long = False
            else:
                continue

            entry_price = (
                pos.get("entryPrice")
                or pos.get("avgPx")
                or pos.get("info", {}).get("avgPx")
            )
            if not entry_price:
                continue
            entry_price = float(entry_price)

            ticker = fut_ex.fetch_ticker(symbol)
            last_price = float(ticker["last"])

            if entry_price <= 0:
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

            reason = None
            side = None
            amount = 0.0

            if pnl_pct <= sl:
                reason = f"合约止损: lev={lev:.1f}x, pnl={pnl_pct*100:.2f}%"
                side = "sell" if is_long else "buy"
                amount = abs(size)
            elif pnl_pct >= tp2:
                reason = f"合约止盈2: lev={lev:.1f}x, pnl={pnl_pct*100:.2f}%"
                side = "sell" if is_long else "buy"
                amount = abs(size)
            elif pnl_pct >= tp1:
                reason = f"合约止盈1: lev={lev:.1f}x, pnl={pnl_pct*100:.2f}%"
                side = "sell" if is_long else "buy"
                amount = abs(size) * 0.5
                if amount <= 0:
                    amount = abs(size)

            if reason is None or side is None or amount <= 0:
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
            print(f"[main] 处理持仓止盈止损时出错: {e}")
            continue

    return close_orders


# =============================
# 风控：过滤 / 调整订单
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
    - 去重：同一 market+symbol+side+reduce_only 每轮只保留一单
    - 限制总单数：最多 MAX_ORDERS_PER_RUN
    - 限制单笔规模：
        * 现货：按 USDT 名义金额限制
        * 合约：按 USDT 名义金额限制（使用 contractSize）
    """
    if not orders:
        return []

    # 去重
    if DEDUPE_SAME_SYMBOL_SIDE:
        seen: Dict[tuple, bool] = {}
        deduped: List[OrderRequest] = []
        for o in orders:
            key = (o.market, o.symbol, o.side.value, bool(getattr(o, "reduce_only", False)))
            if key in seen:
                continue
            seen[key] = True
            deduped.append(o)
        orders = deduped

    # 限制总单数
    if len(orders) > MAX_ORDERS_PER_RUN:
        orders = orders[:MAX_ORDERS_PER_RUN]

    adjusted: List[OrderRequest] = []
    for o in orders:
        if o.market == MarketType.SPOT:
            try:
                ticker = spot_ex.fetch_ticker(o.symbol)
                last_price = float(ticker["last"])
                notional = o.amount * last_price
                if last_price > 0 and max_spot_notional > 0 and notional > max_spot_notional:
                    max_amt = max_spot_notional / last_price
                    print(
                        f"[risk] 现货 {o.symbol} 名义金额 {notional:.2f} 超出限制，"
                        f"调整数量为 {max_amt:.6f}"
                    )
                    o.amount = max_amt
            except Exception as e:
                print(f"[risk] 获取现货价格失败 {o.symbol}: {e}")
        else:
            try:
                market = fut_ex.market(o.symbol)
                contract_size = float(
                    market.get("contractSize")
                    or market.get("contractValue")
                    or 1.0
                )
                ticker = fut_ex.fetch_ticker(o.symbol)
                last_price = float(ticker["last"])
                notional = o.amount * contract_size * last_price

                if last_price > 0 and max_fut_notional > 0 and notional > max_fut_notional:
                    max_amt = max_fut_notional / (contract_size * last_price)
                    max_amt = min(max_amt, MAX_FUTURES_CONTRACTS_PER_ORDER)
                    print(
                        f"[risk] 合约 {o.symbol} 名义金额 {notional:.2f} 超出限制，"
                        f"调整张数为 {max_amt:.4f}"
                    )
                    o.amount = max_amt
                elif o.amount > MAX_FUTURES_CONTRACTS_PER_ORDER:
                    print(
                        f"[risk] 合约 {o.symbol} 张数 {o.amount} 超出限制，"
                        f"调整为 {MAX_FUTURES_CONTRACTS_PER_ORDER}"
                    )
                    o.amount = MAX_FUTURES_CONTRACTS_PER_ORDER
            except Exception as e:
                print(f"[risk] 处理合约风险失败 {o.symbol}: {e}")

        adjusted.append(o)

    return adjusted


# =============================
# 金额估算 & WeCom 文本格式
# =============================

def _estimate_notional(
    req: OrderRequest,
    spot_ex: ccxt.Exchange,
    fut_ex: ccxt.Exchange,
) -> float:
    """
    估算下单金额（USDT），用于展示和日志。
    """
    try:
        if req.market == MarketType.SPOT:
            ticker = spot_ex.fetch_ticker(req.symbol)
            last_price = float(ticker["last"])
            return req.amount * last_price
        else:
            market = fut_ex.market(req.symbol)
            contract_size = float(
                market.get("contractSize")
                or market.get("contractValue")
                or 1.0
            )
            ticker = fut_ex.fetch_ticker(req.symbol)
            last_price = float(ticker["last"])
            return req.amount * contract_size * last_price
    except Exception as e:
        print(f"[main] 估算金额失败 {req.symbol}: {e}")
        return 0.0


def _format_wecom_message(req: OrderRequest, success: bool, error: str | None, notional: float) -> str:
    env_tag = "DEMO" if req.env == Env.TEST else "LIVE"
    market_tag = "现货" if req.market == MarketType.SPOT else "合约"

    pos_desc = "现货" if req.market == MarketType.SPOT else "合约"
    status = "✅ 成功" if success else "❌ 失败"

    lines = [
        f"[{env_tag}] [{market_tag}] {status}",
        f"品种: {req.symbol}",
        f"方向: {pos_desc} / {req.side.value}",
    ]

    if notional > 0:
        lines.append(f"金额: ~{notional:.2f} USDT")
    else:
        lines.append(f"数量: {req.amount}")

    if req.leverage is not None and req.market == MarketType.FUTURES:
        lines.append(f"杠杆: {req.leverage}x")

    if req.reason:
        lines.append(f"原因: {req.reason}")

    if not success and error:
        lines.append(f"错误: {error}")

    return "\n".join(lines)


def _send_batch_wecom_message(env: Env, orders: List[OrderRequest], messages: List[str]) -> None:
    if not messages:
        return

    header_env = "DEMO（OKX 模拟盘）" if env == Env.TEST else "LIVE（OKX 实盘）"

    lines = [f"### 交易机器人执行结果 - {header_env}", ""]
    for i, (order, msg) in enumerate(zip(orders, messages), start=1):
        lines.append(f"**#{i} {order.symbol}**")
        lines.append(msg)
        lines.append("")

    text = "\n".join(lines)
    send_wecom_message(text)


# =============================
# 交易日志（胜率评估 / 收益曲线基础）
# =============================

def _append_trade_log(
    env: Env,
    req: OrderRequest,
    success: bool,
    error: str | None,
    notional: float,
) -> None:
    """
    追加一行到 logs/trades.csv：
    timestamp, env, market, symbol, side, notional, leverage, reduce_only, success, error
    """
    try:
        os.makedirs(os.path.dirname(TRADE_LOG_PATH), exist_ok=True)
        is_new = not os.path.exists(TRADE_LOG_PATH)
        with open(TRADE_LOG_PATH, "a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            if is_new:
                writer.writerow([
                    "timestamp",
                    "env",
                    "market",
                    "symbol",
                    "side",
                    "notional",
                    "leverage",
                    "reduce_only",
                    "success",
                    "error",
                ])
            writer.writerow([
                datetime.utcnow().isoformat(),
                env.value,
                req.market.value,
                req.symbol,
                req.side.value,
                f"{notional:.8f}",
                req.leverage if req.leverage is not None else "",
                int(getattr(req, "reduce_only", False)),
                int(success),
                (error or "").replace("\n", " ")[:200],
            ])
    except Exception as e:
        print(f"[log] 写入交易日志失败: {e}")


# =============================
# 实盘安全检查（强化 B）
# =============================

def _run_live_sanity_checks(
    spot_ex: ccxt.Exchange,
    fut_ex: ccxt.Exchange,
) -> bool:
    """
    LIVE 实盘安全检查：
    - 尝试拉取余额（验证密钥和权限）
    - 尝试拉取一个行情/市场（验证网络/交易所状态）

    返回 True 表示可以继续交易；False 表示本次 run-bot 直接退出。
    """
    print("[sanity] 正在执行实盘安全检查...")

    # 1. 余额检查
    try:
        bal = spot_ex.fetch_balance()
        usdt = bal.get("USDT") or {}
        total = float(usdt.get("total") or 0.0)
        free = float(usdt.get("free") or 0.0)
        print(f"[sanity] LIVE 余额检查: total={total:.2f}, free={free:.2f}")
    except Exception as e:
        print(f"[sanity] LIVE 余额检查失败: {e}")
        return False

    # 2. 行情 / 市场检查
    try:
        markets = fut_ex.load_markets()
        if "BTC/USDT:USDT" in markets:
            print("[sanity] 市场检查: BTC/USDT:USDT 合约可用。")
        else:
            print("[sanity] 市场检查: 未找到 BTC/USDT:USDT 合约。")
    except Exception as e:
        print(f"[sanity] 市场检查失败: {e}")
        return False

    print("[sanity] 实盘安全检查通过，可以执行策略。")
    return True


# =============================
# 主入口
# =============================

def run_once() -> None:
    env = _load_env()
    trader = _build_trader()
    spot_ex, fut_ex = _build_okx_clients(env)

    print(f"[main] 运行环境: {env.value}")
    if env == Env.TEST:
        print("[notice] 当前为 DEMO 模拟盘环境，"
              "策略与风控主要用于回测和调试，请勿视为真实盈亏。")

    # LIVE 环境：先跑安全检查
    if env == Env.LIVE:
        if not _run_live_sanity_checks(spot_ex, fut_ex):
            print("[main] 实盘安全检查未通过，本次不执行任何交易。")
            return

    # 0. 查询余额
    total_usdt, free_usdt = _fetch_usdt_balance(env, spot_ex)
    print(f"[main] 账户 USDT 余额: total={total_usdt:.2f}, free={free_usdt:.2f}")

    # 计算名义金额上限（动态仓位管理）
    max_spot_notional, max_fut_notional = _get_notional_limits(env, total_usdt)
    print(
        f"[main] 名义金额上限: "
        f"spot≈{max_spot_notional:.2f}U, futures≈{max_fut_notional:.2f}U"
    )

    # 1. 生成“合约止盈/止损/分批平仓订单”
    close_orders = _generate_futures_close_orders(env, fut_ex)
    if close_orders:
        print(f"[main] 检测到 {len(close_orders)} 个平仓订单。")

    # 2. 余额不足时，不再开新仓（DEMO 伪余额模式不会触发）
    can_open_new = True
    if env == Env.LIVE:
        can_open_new = free_usdt >= MIN_USDT_FREE_FOR_OPEN
        if not can_open_new:
            print(
                f"[main] LIVE 可用余额 {free_usdt:.2f} USDT 低于阈值 "
                f"{MIN_USDT_FREE_FOR_OPEN}, 本轮仅允许平仓，不开新仓。"
            )

    # 3. 策略生成新的开仓订单
    open_orders: List[OrderRequest] = []
    if can_open_new:
        open_orders = strategy.generate_orders(env)

    if not close_orders and not open_orders:
        print("[main] 本次无交易信号，退出。")
        return

    all_orders = close_orders + open_orders
    print(f"[main] 仓位管理 + 策略 共生成 {len(all_orders)} 个原始订单。")

    # 4. 应用基础风控
    orders = _apply_risk_controls(
        env,
        all_orders,
        spot_ex,
        fut_ex,
        max_spot_notional,
        max_fut_notional,
    )
    print(f"[main] 风控后实际执行 {len(orders)} 个订单。")

    if not orders:
        print("[main] 风控过滤掉所有订单，本次不交易。")
        return

    # 5. 下单并记录结果
    wecom_messages: List[str] = []

    for idx, req in enumerate(orders, start=1):
        print(
            f"[main] ({idx}/{len(orders)}) 下单: "
            f"{req.market.value} {req.symbol} {req.side.value} {req.amount}"
        )
        res = trader.place_order(req)

        notional = _estimate_notional(req, spot_ex, fut_ex)
        msg = _format_wecom_message(req, res.success, res.error, notional)
        wecom_messages.append(msg)

        print(msg)

        # 写入交易日志（胜率 / 收益曲线后续都用它）
        _append_trade_log(env, req, res.success, res.error, notional)

    # 6. 推送企业微信
    try:
        _send_batch_wecom_message(env, orders, wecom_messages)
        print("[main] 企业微信推送已发送。")
    except Exception as e:
        print(f"[main] 企业微信推送失败: {e}")


if __name__ == "__main__":
    run_once()
