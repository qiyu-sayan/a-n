# bot/main.py
"""
交易机器人主入口（OKX + 企业微信）

职责：
- 根据 BOT_ENV 选择 DEMO / LIVE
- 初始化 OKX Trader
- 调用策略生成订单
- 应用基础风控过滤 / 调整订单
- 调用 Trader 下单
- 把执行结果推送到企业微信
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

# 现货单笔最大名义金额（约，USDT）
MAX_SPOT_NOTIONAL_USDT = 20.0

# 合约单笔最大张数
MAX_FUTURES_CONTRACTS_PER_ORDER = 2


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
# 风控：过滤 / 调整订单
# =============================

def _build_okx_clients_for_risk() -> Tuple[ccxt.Exchange, ccxt.Exchange]:
    spot = ccxt.okx()
    spot.options["defaultType"] = "spot"

    swap = ccxt.okx()
    swap.options["defaultType"] = "swap"

    return spot, swap


def _apply_risk_controls(env: Env, orders: List[OrderRequest]) -> List[OrderRequest]:
    """
    对策略生成的订单做基础风控：

    - 去重：同一 market+symbol+side 每轮只保留一单
    - 限制总单数：最多 MAX_ORDERS_PER_RUN
    - 限制单笔规模：现货按 USDT 名义金额限制，合约按张数限制
    """
    if not orders:
        return []

    # LIVE 环境以后可以加更严格控制，目前主要针对 TEST
    # 去重
    if DEDUPE_SAME_SYMBOL_SIDE:
        seen: Dict[Tuple[MarketType, str, str], bool] = {}
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

    # 现货名义金额 / 合约张数限制
    spot_ex, fut_ex = _build_okx_clients_for_risk()

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
            if o.amount > MAX_FUTURES_CONTRACTS_PER_ORDER:
                print(
                    f"[risk] 合约 {o.symbol} 张数 {o.amount} 超出限制，"
                    f"调整为 {MAX_FUTURES_CONTRACTS_PER_ORDER}"
                )
                o.amount = MAX_FUTURES_CONTRACTS_PER_ORDER

        adjusted.append(o)

    return adjusted


# =============================
# 企业微信推送
# =============================

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

    print(f"[main] 运行环境: {env.value}")

    # 1. 策略生成订单
    raw_orders: List[OrderRequest] = strategy.generate_orders(env)

    if not raw_orders:
        print("[main] 本次无交易信号，退出。")
        return

    print(f"[main] 策略生成 {len(raw_orders)} 个原始订单。")

    # 2. 应用基础风控
    orders = _apply_risk_controls(env, raw_orders)
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
        msg = Trader.format_wecom_message(req, res)
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
