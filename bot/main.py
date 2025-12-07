# bot/main.py
"""
交易机器人主入口（OKX + 企业微信）

职责：
- 根据环境变量选择 DEMO / LIVE
- 初始化 OKX Trader
- 调用策略模块生成订单请求
- 调用 Trader 下单
- 把执行结果推送到企业微信（作为“交易界面”）
"""

import os
from typing import List

from bot.trader import (
    Trader,
    Env,
    OrderRequest,
)
from bot.wecom_notify import send_wecom_message
from bot import strategy


def _load_env() -> Env:
    """
    通过环境变量 BOT_ENV 选择运行环境：
        BOT_ENV = "live"  -> Env.LIVE  (实盘)
        其它 / 未配置      -> Env.TEST  (OKX 模拟盘)
    """
    env_str = os.getenv("BOT_ENV", "test").lower()
    if env_str == "live":
        return Env.LIVE
    return Env.TEST


def _build_trader() -> Trader:
    """
    从环境变量加载 OKX 模拟盘 & 实盘 API，构造 Trader。
    （Secrets 已经在 GitHub Actions 里配置好）
    """
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


def _send_batch_wecom_message(env: Env, orders: List[OrderRequest], messages: List[str]) -> None:
    """
    把本次运行的所有订单结果合并成一条企业微信消息（方便你一眼看完）。
    推送风格：按你的要求，尽量“极简”。
    """
    if not messages:
        return

    header_env = "DEMO（OKX 模拟盘）" if env == Env.TEST else "LIVE（OKX 实盘）"

    body_lines = [f"### 交易机器人执行结果 - {header_env}", ""]
    for i, (order, msg) in enumerate(zip(orders, messages), start=1):
        body_lines.append(f"**#{i} {order.symbol}**")
        body_lines.append(msg)
        body_lines.append("")  # 空行分隔

    text = "\n".join(body_lines)
    send_wecom_message(text)


def run_once() -> None:
    """
    执行一次完整的交易流程：生成信号 -> 下单 -> 推送。
    后面 GitHub Actions 的 run-bot 就是定时调用这个入口。
    """
    env = _load_env()
    trader = _build_trader()

    print(f"[main] 运行环境: {env.value}")

    # 1. 让策略模块生成本次要执行的订单（可能是现货 + 合约混合）
    orders: List[OrderRequest] = strategy.generate_orders(env)

    if not orders:
        print("[main] 本次无交易信号，退出。")
        # 也可以选择发一条“无交易”的企业微信，这里先不打扰你
        return

    print(f"[main] 本次共有 {len(orders)} 个订单需要执行。")

    # 2. 逐个下单，并记录结果
    wecom_messages: List[str] = []

    for idx, req in enumerate(orders, start=1):
        print(f"[main] ({idx}/{len(orders)}) 下单: {req.market.value} {req.symbol} {req.side.value} {req.amount}")
        res = trader.place_order(req)

        # 用 Trader 自带的极简格式化方法
        msg = Trader.format_wecom_message(req, res)
        wecom_messages.append(msg)

        # 控制台也打印一下
        print(msg)

    # 3. 合并推送到企业微信
    try:
        _send_batch_wecom_message(env, orders, wecom_messages)
        print("[main] 企业微信推送已发送。")
    except Exception as e:
        print(f"[main] 企业微信推送失败: {e}")


if __name__ == "__main__":
    run_once()
