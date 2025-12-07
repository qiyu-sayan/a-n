from __future__ import annotations

import os
import csv
from datetime import datetime
from typing import List, Tuple, Dict, Optional

import ccxt

from bot.trader import Trader, Env, MarketType, OrderRequest, Side
from bot.wecom_notify import send_wecom_message
from bot import strategy

# =============================
# 全局参数（可以按需微调）
# =============================

# 每次 run-bot 最多执行多少个订单（现货 + 合约）
MAX_ORDERS_PER_RUN = 6

# DEMO 单笔名义金额上限（USDT）
MAX_SPOT_NOTIONAL_TEST = 10.0
MAX_FUTURES_NOTIONAL_TEST = 10.0

# LIVE 单笔名义金额上限（USDT）
MAX_SPOT_NOTIONAL_LIVE = 10.0
MAX_FUTURES_NOTIONAL_LIVE = 10.0

# LIVE：单笔风险占总资产比例上限（例如 0.02 = 2%）
LIVE_RISK_PER_TRADE = 0.02

# LIVE：余额低于这个值就不再开新仓（只允许平仓）
MIN_USDT_FREE_FOR_OPEN = 20.0

# 交易日志路径（相对仓库根目录）
TRADE_LOG_PATH = os.path.join(os.path.dirname(__file__), "..", "logs", "trades.csv")


# =============================
# 环境 & Trader 初始化
# =============================

def _load_env() -> Env:
    """从环境变量 BOT_ENV 推断当前环境：test / live"""
    env_str = os.getenv("BOT_ENV", "test").lower()
    if env_str == "live":
        return Env.LIVE
    return Env.TEST


def _build_trader() -> Trader:
    """基于 OKX 的模拟盘 / 实盘密钥创建 Trader 封装"""
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
    return Trader("okx", paper_keys=paper_keys, live_keys=live_keys)


def _build_okx_clients(env: Env) -> Tuple[ccxt.Exchange, ccxt.Exchange]:
    """
    创建 ccxt OKX 客户端：
    - spot：现货
    - fut：永续合约（swap）
    """
    if env == Env.TEST:
        cfg = {
            "apiKey": os.getenv("OKX_PAPER_API_KEY"),
            "secret": os.getenv("OKX_PAPER_API_SECRET"),
            "password": os.getenv("OKX_PAPER_API_PASSPHRASE"),
            "headers": {"x-simulated-trading": "1"},
        }
    else:
        cfg = {
            "apiKey": os.getenv("OKX_LIVE_API_KEY"),
            "secret": os.getenv("OKX_LIVE_API_SECRET"),
            "password": os.getenv("OKX_LIVE_API_PASSPHRASE"),
        }

    spot = ccxt.okx(cfg)
    spot.options["defaultType"] = "spot"

    fut = ccxt.okx(cfg)
    fut.options["defaultType"] = "swap"

    try:
        fut.load_markets()
    except Exception as e:
        print(f"[main] 加载合约市场信息失败: {e}")

    return spot, fut


# =============================
# 余额 & 名义金额上限
# =============================

def _is_demo_feature_unavailable(e: Exception) -> bool:
    msg = str(e)
    return "50038" in msg and "unavailable in demo trading" in msg


def _fetch_usdt_balance(env: Env, spot: ccxt.Exchange) -> Tuple[float, float]:
    """
    返回 (total, free) USDT 余额。
    DEMO：如果接口不可用，则启用“伪余额模式”，余额当作非常大，只用于控制下单金额。
    """
    try:
        bal = spot.fetch_balance()
        usdt = bal.get("USDT") or {}
        return float(usdt.get("total") or 0.0), float(usdt.get("free") or 0.0)
    except Exception as e:
        print(f"[main] 获取余额失败: {e}")
        if env == Env.TEST and _is_demo_feature_unavailable(e):
            print("[notice] 当前为 DEMO 模拟盘，余额接口不可用，"
                  "启用『伪余额模式』：假定余额非常充足，仅用于调试策略。")
            return 9_999_999.0, 9_999_999.0
        return 0.0, 0.0


def _get_notional_limits(env: Env, total_usdt: float) -> Tuple[float, float]:
    """
    返回 (现货名义金额上限, 合约名义金额上限)
    DEMO：使用固定上限
    LIVE：使用 min(绝对上限, 总资产 * 风险比例)
    """
    if env == Env.TEST:
        return MAX_SPOT_NOTIONAL_TEST, MAX_FUTURES_NOTIONAL_TEST

    total_usdt = max(0.0, float(total_usdt))
    if total_usdt <= 0:
        return 0.0, 0.0

    dynamic_cap = total_usdt * LIVE_RISK_PER_TRADE
    max_spot = min(MAX_SPOT_NOTIONAL_LIVE, dynamic_cap)
    max_fut = min(MAX_FUTURES_NOTIONAL_LIVE, dynamic_cap)
    return max_spot, max_fut


# =============================
# 风控：去重 + 限单数 + 限金额
# =============================

def _apply_risk_controls(
    env: Env,
    orders: List[OrderRequest],
    spot: ccxt.Exchange,
    fut: ccxt.Exchange,
    max_spot_notional: float,
    max_fut_notional: float,
) -> List[OrderRequest]:
    if not orders:
        return []

    # 1. 去重同一 market + symbol + side + reduce_only
    seen: Dict[tuple, bool] = {}
    deduped: List[OrderRequest] = []
    for o in orders:
        key = (o.market, o.symbol, o.side.value, bool(getattr(o, "reduce_only", False)))
        if key in seen:
            continue
        seen[key] = True
        deduped.append(o)

    # 2. 限制本轮总单数
    orders = deduped[:MAX_ORDERS_PER_RUN]

    # 3. 控制每单名义金额
    adjusted: List[OrderRequest] = []
    for o in orders:
        try:
            if o.market == MarketType.SPOT:
                # 现货：数量 * last_price
                ticker = spot.fetch_ticker(o.symbol)
                last_price = float(ticker["last"])
                if last_price > 0 and max_spot_notional > 0:
                    notional = o.amount * last_price
                    if notional > max_spot_notional:
                        max_amt = max_spot_notional / last_price
                        print(
                            f"[risk] 现货 {o.symbol} 名义金额 {notional:.2f} 超出限制，"
                            f"调整数量为 {max_amt:.6f}"
                        )
                        o.amount = max_amt
            else:
                # 合约：张数 * contractSize * last_price
                market = fut.market(o.symbol)
                contract_size = float(
                    market.get("contractSize")
                    or market.get("contractValue")
                    or 1.0
                )
                ticker = fut.fetch_ticker(o.symbol)
                last_price = float(ticker["last"])
                if last_price > 0 and max_fut_notional > 0:
                    notional = o.amount * contract_size * last_price
                    if notional > max_fut_notional:
                        max_amt = max_fut_notional / (contract_size * last_price)

                        # 考虑合约最小下单量限制（很多合约允许小数张）
                        limits = market.get("limits") or {}
                        amt_limits = limits.get("amount") or {}
                        min_amt = float(amt_limits.get("min") or 0.0)
                        if min_amt and max_amt < min_amt:
                            print(
                                f"[risk] 合约 {o.symbol} 计算得到的数量 {max_amt:.6f} "
                                f"低于交易所最小下单量 {min_amt}, 使用最小下单量。"
                            )
                            max_amt = min_amt

                        print(
                            f"[risk] 合约 {o.symbol} 名义金额 {notional:.2f} 超出限制，"
                            f"调整张数为 {max_amt:.6f}"
                        )
                        o.amount = max_amt
        except Exception as e:
            print(f"[risk] 处理风险失败 {o.symbol}: {e}")

        adjusted.append(o)

    return adjusted


# =============================
# 金额估算 & WeCom 文本
# =============================

def _estimate_notional(req: OrderRequest, spot: ccxt.Exchange, fut: ccxt.Exchange) -> float:
    """估算下单金额（USDT），用于展示和日志."""
    try:
        if req.market == MarketType.SPOT:
            t = spot.fetch_ticker(req.symbol)
            return float(t["last"]) * req.amount
        else:
            m = fut.market(req.symbol)
            cs = float(m.get("contractSize") or m.get("contractValue") or 1.0)
            t = fut.fetch_ticker(req.symbol)
            return float(t["last"]) * req.amount * cs
    except Exception as e:
        print(f"[main] 估算金额失败 {req.symbol}: {e}")
        return 0.0


def _format_wecom_message(
    req: OrderRequest,
    success: bool,
    error: Optional[str],
    notional: float,
) -> str:
    """把一笔订单格式化成企业微信文本。"""
    env_tag = "DEMO" if req.env == Env.TEST else "LIVE"
    market_tag = "现货" if req.market == MarketType.SPOT else "合约"
    pos_desc = "现货" if req.market == MarketType.SPOT else "合约"
    status = "✅ 成功" if success else "❌ 失败"

    lines = [
        f"[{env_tag}] [{market_tag}] {status}",
        f"品种: {req.symbol}",
        f"方向: {pos_desc} / {req.side.value}",
        f"数量: {req.amount}",
    ]

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


def _send_batch_wecom(env: Env, orders: List[OrderRequest], messages: List[str]) -> None:
    """把一整轮结果打包发企业微信。"""
    if not messages:
        return
    header_env = "DEMO（OKX 模拟盘）" if env == Env.TEST else "LIVE（OKX 实盘）"
    lines = [f"### 交易机器人执行结果 - {header_env}", ""]
    for i, (o, msg) in enumerate(zip(orders, messages), start=1):
        lines.append(f"# {i} {o.symbol}")
        lines.append(msg)
        lines.append("")
    send_wecom_message("\n".join(lines))


# =============================
# 交易日志（后面算胜率 / 曲线用）
# =============================

def _append_trade_log(
    env: Env,
    req: OrderRequest,
    success: bool,
    error: Optional[str],
    notional: float,
) -> None:
    """追加一行到 logs/trades.csv."""
    try:
        os.makedirs(os.path.dirname(TRADE_LOG_PATH), exist_ok=True)
        is_new = not os.path.exists(TRADE_LOG_PATH)
        with open(TRADE_LOG_PATH, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if is_new:
                w.writerow([
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
            w.writerow([
                datetime.utcnow().isoformat(),
                env.value,
                req.market.value,
                req.symbol,
                req.side.value,
                f"{notional:.8f}",
                req.leverage if req.leverage is not None else "",
                int(getattr(req, "reduce_only", False)),
                int(success),
                (error or "").replace("\n", " ")[:180],
            ])
    except Exception as e:
        print(f"[log] 写入交易日志失败: {e}")


# =============================
# 主入口
# =============================

def run_once() -> None:
    env = _load_env()
    trader = _build_trader()
    spot, fut = _build_okx_clients(env)

    print(f"[main] 运行环境: {env.value}")
    if env == Env.TEST:
        print("[notice] 当前为 DEMO 模拟盘，自动止盈止损功能受限，"
              "主要用于测试策略和信号。")

    # 1. 余额 & 名义金额上限
    total_usdt, free_usdt = _fetch_usdt_balance(env, spot)
    print(f"[main] 账户 USDT 余额: total={total_usdt:.2f}, free={free_usdt:.2f}")

    max_spot_notional, max_fut_notional = _get_notional_limits(env, total_usdt)
    print(
        f"[main] 名义金额上限: "
        f"spot≈{max_spot_notional:.2f}U, futures≈{max_fut_notional:.2f}U"
    )

    can_open_new = True
    if env == Env.LIVE and free_usdt < MIN_USDT_FREE_FOR_OPEN:
        can_open_new = False
        print(
            f"[main] LIVE 可用余额 {free_usdt:.2f} USDT 低于阈值 "
            f"{MIN_USDT_FREE_FOR_OPEN}, 本轮仅允许平仓。"
        )

    # DEMO：暂不做自动止盈 / 止损（因为拿不到持仓）
    close_orders: List[OrderRequest] = []

    # 2. 策略生成开仓订单
    open_orders: List[OrderRequest] = []
    if can_open_new:
        open_orders = strategy.generate_orders(env)

    if not close_orders and not open_orders:
        print("[main] 本次无交易信号，退出。")
        return

    all_orders = close_orders + open_orders
    print(f"[main] 原始订单数: {len(all_orders)}")

    # 3. 风控处理
    orders = _apply_risk_controls(
        env, all_orders, spot, fut, max_spot_notional, max_fut_notional
    )
    print(f"[main] 风控后执行订单数: {len(orders)}")

    if not orders:
        print("[main] 风控过滤后无订单，本次不交易。")
        return

    # 4. 下单 + 日志 + WeCom
    wecom_msgs: List[str] = []
    for idx, req in enumerate(orders, start=1):
        print(
            f"[main] ({idx}/{len(orders)}) 下单: "
            f"{req.market.value} {req.symbol} {req.side.value} {req.amount}"
        )
        res = trader.place_order(req)
        notional = _estimate_notional(req, spot, fut)
        msg = _format_wecom_message(req, res.success, res.error, notional)
        wecom_msgs.append(msg)
        print(msg)
        _append_trade_log(env, req, res.success, res.error, notional)

    try:
        _send_batch_wecom(env, orders, wecom_msgs)
        print("[main] 企业微信推送已发送。")
    except Exception as e:
        print(f"[main] 企业微信推送失败: {e}")


if __name__ == "__main__":
    run_once()
