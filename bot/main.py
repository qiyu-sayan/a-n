#!/usr/bin/env python
# -*- coding: utf-8 -*-


import os
import json
import math
from pathlib import Path
from datetime import datetime, timezone
from typing import Dict, Any

from binance.client import Client
from binance.exceptions import BinanceAPIException, BinanceRequestException

from wecom_notify import wecom_notify  # 你原来的推送模块

BASE_DIR = Path(__file__).resolve().parent
STATE_FILE = BASE_DIR / "state.json"


# ---------- 工具函数 ----------

def env_bool(name: str, default: bool = False) -> bool:
    v = os.getenv(name)
    if v is None or v == "":
        return default
    return v.strip().lower() in ("1", "true", "yes", "y", "on")


def env_float(name: str, default: float) -> float:
    v = os.getenv(name)
    if v is None or v.strip() == "":
        return float(default)
    try:
        return float(v)
    except ValueError:
        return float(default)


def load_state() -> Dict[str, Any]:
    if not STATE_FILE.exists():
        return {}
    try:
        with STATE_FILE.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state: Dict[str, Any]) -> None:
    try:
        with STATE_FILE.open("w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"保存 state.json 失败: {e}")


def round_step(qty: float, step: float) -> float:
    if step <= 0:
        return qty
    return math.floor(qty / step) * step


# ---------- 配置 & 客户端 ----------

def load_config() -> Dict[str, Any]:
    symbols_raw = os.getenv("SYMBOLS", "BTCUSDT")
    symbols = [s.strip().upper() for s in symbols_raw.split(",") if s.strip()]

    cfg = {
        "api_key": os.getenv("BINANCE_KEY", "").strip(),
        "api_secret": os.getenv("BINANCE_SECRET", "").strip(),
        "symbols": symbols,
        "enable_trading": env_bool("ENABLE_TRADING", True),
        "paper_trading": env_bool("PAPER", False),
        "order_usdt": env_float("ORDER_USDT", 10.0),
        "take_profit_pct": env_float("TAKE_PROFIT_PCT", 2.0) / 100.0,  # 2 -> 0.02
        "stop_loss_pct": env_float("STOP_LOSS_PCT", 1.0) / 100.0,     # 1 -> 0.01
        "risk_limit_usdt": env_float("RISK_LIMIT_USDT", 200.0),
    }
    return cfg


def make_client(cfg: Dict[str, Any]) -> Client:
    if not cfg["api_key"] or not cfg["api_secret"]:
        raise RuntimeError("BINANCE_KEY / BINANCE_SECRET 未配置")

    # demo.binance.com 使用正式 API 域名，但账号是模拟盘
    client = Client(cfg["api_key"], cfg["api_secret"])
    # 测试连通性
    client.ping()
    return client


def load_symbol_meta(client: Client, symbols: list[str]) -> Dict[str, Dict[str, Any]]:
    """
    获取每个交易对的元数据：baseAsset、LOT_SIZE 步长等
    用于计算下单数量
    """
    meta: Dict[str, Dict[str, Any]] = {}
    for sym in symbols:
        info = client.get_symbol_info(sym)
        if not info:
            print(f"{sym}: get_symbol_info 返回空，跳过这个交易对")
            continue

        base_asset = info["baseAsset"]
        lot_filter = None
        for f in info["filters"]:
            if f.get("filterType") == "LOT_SIZE":
                lot_filter = f
                break

        step_size = float(lot_filter["stepSize"]) if lot_filter else 0.00001
        meta[sym] = {
            "base_asset": base_asset,
            "step_size": step_size,
        }
    return meta


# ---------- 策略逻辑 ----------

def get_latest_price(client: Client, symbol: str) -> float:
    ticker = client.get_symbol_ticker(symbol=symbol)
    return float(ticker["price"])


def get_ma20(client: Client, symbol: str) -> float:
    # 最近 20 根 1min K 线
    klines = client.get_klines(
        symbol=symbol, interval=Client.KLINE_INTERVAL_1MINUTE, limit=20
    )
    closes = [float(k[4]) for k in klines]
    if not closes:
        return 0.0
    return sum(closes) / len(closes)


def handle_symbol(
    client: Client,
    cfg: Dict[str, Any],
    state: Dict[str, Any],
    meta_map: Dict[str, Dict[str, Any]],
    symbol: str,
    lines: list[str],
) -> None:
    """
    对单个交易对执行一次“检查行情+买卖”的完整流程
    lines 用于汇总日志，最后推送到企业微信
    """
    try:
        price = get_latest_price(client, symbol)
    except Exception as e:
        lines.append(f"{symbol}: 获取最新价格失败: {e}")
        return

    lines.append(f"{symbol} 最新价格: {price:.4f}")

    symbol_state = state.get(symbol, {"position": "FLAT"})
    position = symbol_state.get("position", "FLAT")
    entry_price = float(symbol_state.get("entry_price", 0.0) or 0.0)
    qty_held_state = float(symbol_state.get("qty", 0.0) or 0.0)

    meta = meta_map.get(symbol)
    if not meta:
        lines.append(f"{symbol}: 没有元数据，跳过")
        return

    base_asset = meta["base_asset"]
    step_size = meta["step_size"]

    # 当前真实持仓（demo 账户）
    try:
        bal = client.get_asset_balance(asset=base_asset)
        real_qty = float(bal["free"])
    except Exception:
        real_qty = 0.0

    # ------------ 无仓 → 考虑开多 ------------
    if position == "FLAT" or real_qty <= 0:
        try:
            ma20 = get_ma20(client, symbol)
        except Exception as e:
            lines.append(f"{symbol}: 获取 MA20 失败: {e}")
            return

        lines.append(f"{symbol} MA20: {ma20:.4f}")

        # 简单规则：价格高于 MA20 0.1% 以上，视为向上突破，开多
        if ma20 <= 0 or price <= ma20 * 1.001:
            lines.append(f"{symbol}: 尚未形成向上突破信号，保持空仓")
            state[symbol] = {"position": "FLAT"}
            return

        # 风险限制：当前敞口 + 本次下单金额 不超过 RISK_LIMIT_USDT
        exposure_now = real_qty * price
        if exposure_now + cfg["order_usdt"] > cfg["risk_limit_usdt"]:
            lines.append(
                f"{symbol}: 当前敞口约 {exposure_now:.2f} USDT，"
                f"超过风险限制 {cfg['risk_limit_usdt']:.2f}，不再加仓"
            )
            return

        # 计算下单数量
        order_usdt = cfg["order_usdt"]
        raw_qty = order_usdt / price
        qty = round_step(raw_qty, step_size)
        if qty <= 0:
            lines.append(f"{symbol}: 计算出的下单数量过小（{raw_qty}），跳过")
            return

        if cfg["enable_trading"] and not cfg["paper_trading"]:
            try:
                order = client.create_order(
                    symbol=symbol,
                    side="BUY",
                    type="MARKET",
                    quantity=qty,
                )
                lines.append(
                    f"{symbol}: ✅ 实盘买入成功 qty={qty}, 约 {order_usdt:.2f} USDT"
                )
            except BinanceAPIException as e:
                lines.append(f"{symbol}: ❌ 买入失败: {e}")
                return
            except BinanceRequestException as e:
                lines.append(f"{symbol}: ❌ 买入请求异常: {e}")
                return
        else:
            lines.append(
                f"{symbol}: [PAPER] 模拟买入 qty={qty}, 金额约 {order_usdt:.2f} USDT"
            )

        # 更新本地 state
        state[symbol] = {
            "position": "LONG",
            "entry_price": price,
            "qty": qty,
        }
        return

    # ------------ 有仓 → 检查止盈 / 止损 ------------
    tp_pct = cfg["take_profit_pct"]
    sl_pct = cfg["stop_loss_pct"]
    take_profit_price = entry_price * (1.0 + tp_pct)
    stop_loss_price = entry_price * (1.0 - sl_pct)

    lines.append(
        f"{symbol}: 持仓中 entry={entry_price:.4f}, "
        f"TP={take_profit_price:.4f} (+{tp_pct*100:.2f}%), "
        f"SL={stop_loss_price:.4f} (-{sl_pct*100:.2f}%)"
    )

    should_sell = False
    reason = ""
    if price >= take_profit_price:
        should_sell = True
        reason = "触发止盈"
    elif price <= stop_loss_price:
        should_sell = True
        reason = "触发止损"

    if not should_sell:
        lines.append(f"{symbol}: 暂未触发止盈/止损，继续持有")
        # 同步一下真实仓位数量
        state[symbol] = {
            "position": "LONG",
            "entry_price": entry_price,
            "qty": real_qty or qty_held_state,
        }
        return

    # 计算卖出数量：取“本地记录的数量”和“真实可用数量”两者的较小值
    sell_qty_raw = min(qty_held_state if qty_held_state > 0 else real_qty, real_qty)
    sell_qty = round_step(sell_qty_raw, step_size)
    if sell_qty <= 0:
        lines.append(f"{symbol}: {reason}，但没有可卖数量，强制标记为空仓")
        state[symbol] = {"position": "FLAT"}
        return

    if cfg["enable_trading"] and not cfg["paper_trading"]:
        try:
            client.create_order(
                symbol=symbol,
                side="SELL",
                type="MARKET",
                quantity=sell_qty,
            )
            lines.append(f"{symbol}: ✅ 卖出成功 qty={sell_qty}, {reason}")
        except BinanceAPIException as e:
            lines.append(f"{symbol}: ❌ 卖出失败: {e}")
            return
        except BinanceRequestException as e:
            lines.append(f"{symbol}: ❌ 卖出请求异常: {e}")
            return
    else:
        lines.append(f"{symbol}: [PAPER] 模拟卖出 qty={sell_qty}, {reason}")

    # 卖出后标记为空仓
    state[symbol] = {"position": "FLAT"}


# ---------- 主流程 ----------

def run_bot() -> bool:
    cfg = load_config()

    now = datetime.now(timezone.utc)
    print("📌 Bot 开始运行")
    print(f"时间: {now.strftime('%Y-%m-%d %H:%M:%S%z')}")
    print("环境: DEMO (币安模拟盘 / demo.binance.com, 使用正式 API 域名)")
    print("REST API 地址: https://api.binance.com")
    print(f"ENABLE_TRADING: {cfg['enable_trading']}")
    print(f"PAPER_TRADING: {cfg['paper_trading']}")
    print(f"每笔下单 USDT: {cfg['order_usdt']} (目前根据策略条件才会下单)")
    print(f"交易标的: {', '.join(cfg['symbols'])}")
    print("-" * 60)

    lines: list[str] = []

    try:
        client = make_client(cfg)
    except Exception as e:
        msg = f"❌ 初始化 Binance 客户端失败: {e}"
        print(msg)
        lines.append(msg)
        summary = "\n".join(lines)
        try:
            wecom_notify(summary)
        except Exception:
            pass
        return False

    state = load_state()
    meta_map = load_symbol_meta(client, cfg["symbols"])

    for sym in cfg["symbols"]:
        print(f"=== 处理交易对: {sym} ===")
        lines.append(f"=== 处理交易对: {sym} ===")
        try:
            handle_symbol(client, cfg, state, meta_map, sym, lines)
        except Exception as e:
            lines.append(f"{sym}: 处理异常: {e}")
        print("-" * 40)

    save_state(state)

    # 运行结果汇总
    lines.append("")
    lines.append("📊 本次运行结果: 详见以上各交易对日志")
    summary = "\n".join(lines)

    try:
        code = wecom_notify(summary)
        print(f"wecom: {code}")
    except Exception as e:
        print(f"发送企业微信通知失败: {e}")

    return True


if __name__ == "__main__":
    ok = False
    try:
        ok = run_bot()
    except Exception as e:
        # 兜底异常
        err_msg = f"run-bot 发生致命异常: {e}"
        print(err_msg)
        try:
            wecom_notify(err_msg)
        except Exception:
            pass

    # warn_451 是你原来用来提醒 451 错误的，这里保留调用
    try:
        wecom_notify()
    except Exception:
        pass

    if not ok:
        raise SystemExit(1)
