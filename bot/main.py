# bot/main.py
"""
交易机器人主入口（OKX + 企业微信）

流程：
1. 根据 BOT_ENV 选择 DEMO / LIVE
2. 初始化 OKX Trader + ccxt 客户端
3. 查询 USDT 余额（total/free）
4. 检查合约持仓，按杠杆动态止盈止损（支持分批止盈） -> 生成平仓订单（reduce_only）
5. 如果余额过低，仅允许平仓，不再开新仓
6. 调用策略生成新的开仓订单（现货 + 合约 + 动态杠杆）
7. 风控：
   - 去重
   - 限制总单数
   - 控制名义金额（DEMO 默认 ~20U / 单，LIVE 预留 ~10U）
8. 下单并估算金额
9. 推送企业微信（金额视角，不带订单号）
"""

import os
from typing import List, Tuple, Dict

import ccxt

from bot.trader import (
    Trader,
    Env,
    MarketType,
    OrderRequest,
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

# 现货 / 合约 单笔最大名义金额（约，USDT）
# DEMO 用更大的名义金额加快测试，LIVE 用更小的保护本金
MAX_SPOT_NOTIONAL_TEST = 20.0
MAX_FUTURES_NOTIONAL_TEST = 20.0

MAX_SPOT_NOTIONAL_LIVE = 10.0
MAX_FUTURES_NOTIONAL_LIVE = 10.0

# 合约单笔最大张数（再保险）
MAX_FUTURES_CONTRACTS_PER_ORDER = 100

# 余额门槛：低于这个值时，不再开新仓，只允许平仓
MIN_USDT_FREE_FOR_OPEN = 20.0


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
            # OKX 模拟盘需要这个头
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

    # 简单检查一下，防止忘记填 secrets
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



def _fetch_usdt_balance(spot_ex: ccxt.Exchange) -> Tuple[float, float]:
    """
    返回 (total, free) USDT 余额。
    """
    try:
        bal = spot_ex.fetch_balance()
        usdt = bal.get("USDT") or {}
        total = float(usdt.get("total") or 0.0)
        free = float(usdt.get("free") or 0.0)
        return total, free
    except Exception as e:
        print(f"[main] 获取余额失败: {e}")
        return 0.0, 0.0


def _get_notional_limits(env: Env) -> Tuple[float, float]:
    if env == Env.TEST:
        return MAX_SPOT_NOTIONAL_TEST, MAX_FUTURES_NOTIONAL_TEST
    else:
        return MAX_SPOT_NOTIONAL_LIVE, MAX_FUTURES_NOTIONAL_LIVE


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
        # 低杠杆：止损宽，止盈更宽，适合吃趋势
        tp1 = 0.05   # +5%
        tp2 = 0.10   # +10%
        sl = -0.03   # -3%
    elif lev <= 10:
        # 中杠杆：止损稍紧，止盈略收窄
        tp1 = 0.04   # +4%
        tp2 = 0.08   # +8%
        sl = -0.02   # -2%
    else:
        # 高杠杆（仅 DEMO）：止损更紧，止盈相对较宽
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
    目前只在 DEMO 环境启用。
    """
    if env != Env.TEST:
        return []

    close_orders: List[OrderRequest] = []

    try:
        positions = fut_ex.fetch_positions(params={"instType": "SWAP"})
    except Exception as e:
        print(f"[main] 获取合约持仓失败: {e}")
        return []

    if not positions:
        return []

    from bot.trader import Side as OrderSide, MarketType, OrderRequest  # 局部导入避免循环

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

            # 杠杆（可能在不同字段里）
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

            # 计算浮动收益率
            if is_long:
                pnl_pct = (last_price - entry_price) / entry_price
            else:
                pnl_pct = (entry_price - last_price) / entry_price

            reason = None
            side = None
            amount = 0.0

            # 止损优先
            if pnl_pct <= sl:
                reason = f"合约止损: lev={lev:.1f}x, pnl={pnl_pct*100:.2f}%"
                side = "sell" if is_long else "buy"
                amount = abs(size)
            # 第二档止盈（平全部）
            elif pnl_pct >= tp2:
                reason = f"合约止盈2: lev={lev:.1f}x, pnl={pnl_pct*100:.2f}%"
                side = "sell" if is_long else "buy"
                amount = abs(size)
            # 第一档止盈（平 50%）
            elif pnl_pct >= tp1:
                reason = f"合约止盈1: lev={lev:.1f}x, pnl={pnl_pct*100:.2f}%"
                side = "sell" if is_long else "buy"
                amount = abs(size) * 0.5
                if amount <= 0:
                    amount = abs(size)

            if reason is None or side is None or amount <= 0:
                continue

            order_side = OrderSide.SELL if side == "sell" else OrderSide.BUY

            close_orders.append(
                OrderRequest(
                    env=env,
                    market=MarketType.FUTURES,
                    symbol=symbol,
                    side=order_side,
                    amount=amount,
                    price=None,
                    leverage=None,   # 平仓不需要设置杠杆
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

    max_spot_notional, max_fut_notional = _get_notional_limits(env)

    adjusted: List[OrderRequest] = []
    for o in orders:
        if o.market == MarketType.SPOT:
            try:
                ticker = spot_ex.fetch_ticker(o.symbol)
                last_price = float(ticker["last"])
                notional = o.amount * last_price
                if notional > max_spot_notional and last_price > 0:
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

                if notional > max_fut_notional and last_price > 0:
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
    估算下单金额（USDT），用于展示。
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
# 主入口
# =============================

def run_once() -> None:
    env = _load_env()
    trader = _build_trader()
    spot_ex, fut_ex = _build_okx_clients()

    print(f"[main] 运行环境: {env.value}")

    # 0. 查询余额
    total_usdt, free_usdt = _fetch_usdt_balance(spot_ex)
    print(f"[main] 账户 USDT 余额: total={total_usdt:.2f}, free={free_usdt:.2f}")

    # 1. 先生成“合约止盈/止损/分批平仓订单”
    close_orders = _generate_futures_close_orders(env, fut_ex)
    if close_orders:
        print(f"[main] 检测到 {len(close_orders)} 个平仓订单。")

    # 2. 余额不足时，不再开新仓
    can_open_new = free_usdt >= MIN_USDT_FREE_FOR_OPEN

    # 3. 策略生成新的开仓订单
    open_orders: List[OrderRequest] = []
    if can_open_new:
        open_orders = strategy.generate_orders(env)
    else:
        print(
            f"[main] 可用余额 {free_usdt:.2f} USDT 低于阈值 {MIN_USDT_FREE_FOR_OPEN}, "
            "本轮仅允许平仓，不开新仓。"
        )

    if not close_orders and not open_orders:
        print("[main] 本次无交易信号，退出。")
        return

    all_orders = close_orders + open_orders
    print(f"[main] 仓位管理 + 策略 共生成 {len(all_orders)} 个原始订单。")

    # 4. 应用基础风控
    orders = _apply_risk_controls(env, all_orders, spot_ex, fut_ex)
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

    # 6. 推送企业微信
    try:
        _send_batch_wecom_message(env, orders, wecom_messages)
        print("[main] 企业微信推送已发送。")
    except Exception as e:
        print(f"[main] 企业微信推送失败: {e}")


if __name__ == "__main__":
    run_once()
