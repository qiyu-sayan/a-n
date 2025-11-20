# bot/main.py

import os
import sys
import time
import math
from datetime import datetime, timezone

from binance.client import Client
from binance.exceptions import BinanceAPIException


# ========= 基本配置 =========

# 交易标的：目前只跑 BTCUSDT
TRADE_SYMBOLS = ["BTCUSDT"]

# 每次计划投入多少 USDT（现在只是打印提示，不会真正下单）
TRADE_USDT = float(os.getenv("TRADE_USDT", "10"))

# 是否真的交易（目前我们默认 False，只做策略判断）
ENABLE_TRADING = os.getenv("ENABLE_TRADING", "false").lower() == "true"

# 是否纸上交易（以后要做内部账户记录可以用上，目前没用）
PAPER_TRADING = os.getenv("PAPER_TRADING", "false").lower() == "true"

# 使用的环境：目前我们只用 DEMO（demo.binance.com）
BINANCE_MODE = os.getenv("BINANCE_MODE", "DEMO").upper()


# ========= 工具函数 =========

def make_client() -> Client:
    """根据环境创建 Binance Client（目前固定用 demo-api.binance.com）"""
    api_key = os.getenv("BINANCE_KEY")
    api_secret = os.getenv("BINANCE_SECRET")

    if not api_key or not api_secret:
        raise RuntimeError("BINANCE_KEY / BINANCE_SECRET 没有配置（Secrets 里忘记填？）")

    base_url = None

    if BINANCE_MODE == "DEMO":
        # 官方给的 Spot Demo Trading 接口地址
        # 参考：https://demo-api.binance.com 
        base_url = "https://demo-api.binance.com"
    elif BINANCE_MODE == "TESTNET":
        base_url = "https://testnet.binance.vision"
    elif BINANCE_MODE == "LIVE":
        base_url = "https://api.binance.com"
    else:
        raise RuntimeError(f"未知 BINANCE_MODE: {BINANCE_MODE}")

    client = Client(api_key, api_secret)
    if base_url:
        client.API_URL = base_url

    return client


def fmt_time(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S%z")


def safe_float(x, default=None):
    try:
        return float(x)
    except Exception:
        return default


# ========= 策略逻辑 =========

def fetch_symbol_info(client: Client, symbol: str):
    """获取交易规则，主要为了知道最小下单数量、精度等（以后真下单会用）"""
    exchange_info = client.get_symbol_info(symbol)
    if not exchange_info:
        raise RuntimeError(f"找不到交易对 {symbol} 的交易规则")

    # 找 LOT_SIZE 规则
    lot_filter = None
    for f in exchange_info.get("filters", []):
        if f.get("filterType") == "LOT_SIZE":
            lot_filter = f
            break

    step_size = safe_float(lot_filter.get("stepSize")) if lot_filter else None
    min_qty = safe_float(lot_filter.get("minQty")) if lot_filter else None

    return {
        "symbol": symbol,
        "step_size": step_size,
        "min_qty": min_qty,
    }


def round_step_size(quantity: float, step_size: float) -> float:
    """按交易所的 stepSize 把数量修正到合法值"""
    if step_size is None or step_size <= 0:
        return quantity
    precision = int(round(-math.log(step_size, 10), 0))
    return float(f"{math.floor(quantity / step_size) * step_size:.{precision}f}")


def get_latest_price(client: Client, symbol: str) -> float:
    ticker = client.get_symbol_ticker(symbol=symbol)
    price = safe_float(ticker.get("price"))
    if price is None:
        raise RuntimeError(f"{symbol} 获取最新价格失败: {ticker}")
    return price


def get_ma_signals(client: Client, symbol: str):
    """
    简单均线策略示例：
    - 拉最近 100 根 1h K 线
    - 算 20MA / 50MA
    - 当前价 vs MA20 给一个建议：买入 / 卖出 / 观望
    """
    klines = client.get_klines(
        symbol=symbol,
        interval=Client.KLINE_INTERVAL_1HOUR,
        limit=100
    )

    closes = [safe_float(k[4]) for k in klines if safe_float(k[4]) is not None]

    if len(closes) < 50:
        raise RuntimeError(f"{symbol} 可用 K 线不足，只有 {len(closes)} 根")

    ma20 = sum(closes[-20:]) / 20
    ma50 = sum(closes[-50:]) / 50
    last_price = closes[-1]

    # 给一个很简单的建议
    # 这里只是示例逻辑，以后可以换成你想要的策略
    advice = "观望"
    reason = "价格在均线附近波动"

    if last_price < ma20 * 0.99:
        advice = "考虑买入"
        reason = "价格低于 MA20 约 1% 以上，可能偏低"
    elif last_price > ma20 * 1.01:
        advice = "考虑卖出"
        reason = "价格高于 MA20 约 1% 以上，可能偏高"

    return {
        "symbol": symbol,
        "price": last_price,
        "ma20": ma20,
        "ma50": ma50,
        "advice": advice,
        "reason": reason,
    }


def run_for_symbol(client: Client, symbol: str) -> str:
    """
    核心流程：
    - 获取规则（将来真实下单会用到）
    - 获取当前价格、均线
    - 根据策略给出建议
    - 目前只打印，不下单
    """
    info = fetch_symbol_info(client, symbol)
    step_size = info["step_size"]
    min_qty = info["min_qty"]

    signal = get_ma_signals(client, symbol)

    price = signal["price"]
    ma20 = signal["ma20"]
    ma50 = signal["ma50"]
    advice = signal["advice"]
    reason = signal["reason"]

    # 如果将来要真的下单，可以估算一下数量（现在只是展示，不执行）
    qty_est = 0.0
    if price and price > 0 and TRADE_USDT > 0:
        qty_est = TRADE_USDT / price
        if step_size:
            qty_est = round_step_size(qty_est, step_size)

    lines = []
    lines.append(f"=== 处理交易对: {symbol} ===")
    lines.append(f"最新价格: {price:.6f}")
    lines.append(f"MA20: {ma20:.6f} | MA50: {ma50:.6f}")
    lines.append(f"策略建议: {advice}（原因：{reason}）")
    if qty_est > 0:
        lines.append(f"按每笔 {TRADE_USDT} USDT 预算，预估下单数量约为: {qty_est}")
        if min_qty and qty_est < min_qty:
            lines.append(
                f"⚠ 预估数量 {qty_est} 小于交易所最小下单量 {min_qty}，将来真下单前需要调大 TRADE_USDT。"
            )

    if ENABLE_TRADING:
        lines.append("当前 ENABLE_TRADING=True，但策略代码里 **还没有** 调用下单接口。")
        lines.append("等你确认策略之后，我们再一起把真实下单逻辑补上。")
    else:
        lines.append("当前不启用真实下单（ENABLE_TRADING=False），本次仅做行情+策略检查。")

    return "\n".join(lines)


def main():
    start = datetime.now(timezone.utc)
    print("💡 Bot 开始运行")
    print(f"时间: {fmt_time(start)}")
    print(f"环境: {BINANCE_MODE} (demo.binance.com)")
    print(f"ENABLE_TRADING: {ENABLE_TRADING}")
    print(f"PAPER_TRADING: {PAPER_TRADING}")
    print(f"每笔下单 USDT: {TRADE_USDT}（目前不会自动下单，只作为预留参数）")
    print(f"交易标的: {', '.join(TRADE_SYMBOLS)}")
    print("-" * 60)

    try:
        client = make_client()
    except Exception as e:
        print(f"❌ 创建 Binance Client 失败: {e}")
        sys.exit(1)

    all_summaries = []

    for symbol in TRADE_SYMBOLS:
        try:
            summary = run_for_symbol(client, symbol)
            print(summary)
            print("-" * 60)
            all_summaries.append(summary)
        except BinanceAPIException as e:
            print(f"❌ {symbol} 处理失败 - BinanceAPIException: {e.status_code} {e.message}")
        except Exception as e:
            print(f"❌ {symbol} 处理失败: {e}")

    print("本次运行结果：")
    for s in all_summaries:
        # 每个 summary 第一行都是 "=== 处理交易对: XXX ==="，就打印这一行代表成功
        first_line = s.splitlines()[0] if s else ""
        print(f"- {first_line} 成功（本次无自动下单逻辑，仅检查行情）")

    end = datetime.now(timezone.utc)
    print(f"✅ run-bot 任务执行完毕，耗时 {int((end - start).total_seconds())} 秒")


if __name__ == "__main__":
    main()
