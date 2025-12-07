# bot/main.py
"""
交易机器人主入口（OKX + 企业微信）

职责：
- 根据 BOT_ENV 选择 DEMO / LIVE
- 初始化 OKX Trader
- 调用策略生成订单
- 应用基础风控过滤 / 调整订单（包含名义金额控制）
- 调用 Trader 下单
- 把执行结果推送到企业微信（显示金额而不是数量）
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
# 基础风控参数
# =============================

# 每次 run-bot 最多执行多少个订单（现货+合约总和）
MAX_ORDERS_PER_RUN = 4

# 同一市场 + 同一 symbol + 同一方向，每轮只保留一单
DEDUPE_SAME_SYMBOL_SIDE = True

# 现货 / 合约 单笔最大名义金额（约，USDT）
MAX_SPOT_NOTIONAL_USDT = 10.0
MAX_FUTURES_NOTIONAL_USDT = 10.0

# 合约单笔最大张数（再保险）
MAX_FUTURES_CONTRACTS_PER_ORDER = 50


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
# OKX 客户端（给风控 & 估算金额用）
# =============================

def _build_okx_clients() -> Tuple[ccxt.Exchange, ccxt.Exchange]:
    spot = ccxt.okx()
    spot.options["defaultType"] = "spot"

    swap = ccxt.okx()
    swap.options["defaultType"] = "swap"

    # 预加载合约市场信息（里面有 contractSize）
    try:
        swap.load_markets()
    except Exception as e:
        print(f"[main] 加载合约市场信息失败: {e}")

    return spot, swap


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
    - 去重：同一 market+symbol+side 每轮只保留一单
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
            key = (o.market, o.symbol, o.side.value)
            if key in seen:
                continue
            seen[key] = True
            deduped.append(o)
        orders = deduped

    # 限制总单数
    if len(orders) > MAX_ORDERS_PER_RUN:
        orders = orders[:MAX_ORDERS_PER_RUN]

    # 名义金额限制
    adjusted: List[OrderRequest] = []
    for o in orders:
        if o.market == MarketType.SPOT:
            try:
                ticker = spot_ex.fetch_ticker(o.symbol)
                last_price = float(ticker["last"])
                notional = o.amount * last_price
                if notional > MAX_SPOT_NOTIONAL_USDT and last_price > 0:
                    max_amt = MAX_SPOT_NOTIONAL_USDT / last_price
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

                if notional > MAX_FUTURES_NOTIONAL_USDT and last_price > 0:
                    max_amt = MAX_FUTURES_NOTIONAL_USDT / (contract_size * last_price)
                    # 再加一个张数上限保护
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
    估算下单金额（USDT）。只是用来展示，不要求和成交成本完全一致。
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

    if req.market == MarketType.SPOT:
        pos_desc = "现货"
    else:
        pos_desc = "合约"

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

    # 1. 策略生成订单
    raw_orders: List[OrderRequest] = strategy.generate_orders(env)

    if not raw_orders:
        print("[main] 本次无交易信号，退出。")
        return

    print(f"[main] 策略生成 {len(raw_orders)} 个原始订单。")

    # 2. 应用基础风控
    orders = _apply_risk_controls(env, raw_orders, spot_ex, fut_ex)
    print(f"[main] 风控后实际执行 {len(orders)} 个订单。")

    if not orders:
        print("[main] 风控过滤掉所有订单，本次不交易。")
        return

    # 3. 下单并记录结果
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

    # 4. 推送企业微信
    try:
        _send_batch_wecom_message(env, orders, wecom_messages)
        print("[main] 企业微信推送已发送。")
    except Exception as e:
        print(f"[main] 企业微信推送失败: {e}")


if __name__ == "__main__":
    run_once()
