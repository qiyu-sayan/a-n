import os
import sys
import time
import traceback
from datetime import datetime, timezone

from binance.client import Client  # 你已经在 requirements.txt 里装了 python-binance
from wecom_notify import wecom_notify  # 和 wecom_notify.py 在同一目录


def env_bool(name: str, default: bool = False) -> bool:
    v = os.getenv(name)
    if v is None or v == "":
        return default
    return str(v).lower() in ("1", "true", "yes", "y", "on")


def env_float(name: str, default: float) -> float:
    v = os.getenv(name)
    if v is None or v == "":
        return default
    try:
        return float(v)
    except Exception:
        return default


def get_symbols() -> list[str]:
    """
    交易对列表：
    - 优先从环境变量 SYMBOLS 读取（逗号分隔，如 "BTCUSDT,ETHUSDT"）
    - 否则默认只交易 BTCUSDT
    """
    raw = os.getenv("SYMBOLS", "").strip()
    if not raw:
        return ["BTCUSDT"]
    return [s.strip().upper() for s in raw.split(",") if s.strip()]


def make_client() -> Client:
    api_key = os.getenv("BINANCE_KEY", "").strip()
    api_secret = os.getenv("BINANCE_SECRET", "").strip()
    if not api_key or not api_secret:
        raise RuntimeError("BINANCE_KEY / BINANCE_SECRET 未配置")

    client = Client(api_key, api_secret)

    # 可选：自定义 API_URL（比如 demo / 代理 等）
    api_url = os.getenv("BINANCE_API_URL", "").strip()
    if api_url:
        # python-binance 用这个字段控制请求地址
        client.API_URL = api_url.rstrip("/") + "/api"

    return client


def describe_env() -> str:
    """打印当前运行环境信息"""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S%z")
    enable_trading = env_bool("ENABLE_TRADING", True)
    paper_trading = env_bool("PAPER", False)
    order_usdt = env_float("ORDER_USDT", 10.0)
    symbols = get_symbols()

    lines = []
    lines.append("📌 Bot 开始运行")
    lines.append(f"时间: {now}")
    lines.append("环境: DEMO(币安模拟盘 / demo.binance.com)")
    lines.append(f"ENABLE_TRADING: {enable_trading}")
    lines.append(f"PAPER_TRADING: {paper_trading}")
    lines.append(f"每笔下单 USDT: {order_usdt}（目前不会自动下单，只作为预留参数）")
    lines.append(f"交易标的: {', '.join(symbols)}")
    lines.append("-" * 60)
    return "\n".join(lines)


# =======================  策略相关（此处先全部不下单）  =======================

def has_long_signal(symbol: str, last_price: float) -> bool:
    """
    这里以后写你的做多信号逻辑。
    现在先固定返回 False —— 也就是说「永远不下单」。
    想开始真实策略时，只需要改这个函数即可。
    """
    return False


def calc_order_quantity_usdt(order_usdt: float, price: float) -> float:
    """根据 USDT 金额和价格计算买入数量（简单除一下，并做一点安全保护）"""
    if price <= 0:
        raise ValueError("价格异常，不能下单")
    qty = order_usdt / price
    # 这里简单保留 6 位小数，后面可以按照交易所 LOT_SIZE 再做精细处理
    return round(qty, 6)


def maybe_trade_symbol(client: Client, symbol: str, enable_trading: bool, order_usdt: float):
    """
    对单个交易对做一次「检查」：
      1. 获取最新价格
      2. 判断是否有信号
      3. 有信号 & 允许交易 -> 下单；否则只打印日志，不下单
    目前 has_long_signal 恒为 False，所以不会真的下单。
    """

    print(f"=== 处理交易对: {symbol} ===")

    # 1. 获取最新价格
    ticker = client.get_symbol_ticker(symbol=symbol)
    last_price = float(ticker["price"])
    print(f"最新价格: {last_price:.6f}")

    # 2. 是否允许真实下单
    if not enable_trading:
        print("ENABLE_TRADING=False，本次仅观察行情，不下单。")
        return

    # 3. 策略信号判断（当前固定为 False）
    if not has_long_signal(symbol, last_price):
        print("暂无交易信号，跳过下单。")
        return

    # 4. 真的要下单时才会走到这里（目前不会走到）
    qty = calc_order_quantity_usdt(order_usdt, last_price)
    if qty <= 0:
        print("计算得到的下单数量 <= 0，跳过。")
        return

    print(f"准备市价买入 {symbol}，约 {order_usdt} USDT，对应数量 ≈ {qty}")
    order = client.order_market_buy(symbol=symbol, quantity=qty)
    print("✅ 下单成功:", order)


# =======================  主入口  =======================

def run_bot():
    msg_lines = []
    enable_trading = env_bool("ENABLE_TRADING", True)
    paper_trading = env_bool("PAPER", False)
    order_usdt = env_float("ORDER_USDT", 10.0)
    symbols = get_symbols()

    print(describe_env())

    client = make_client()

    # 如果以后要支持「纸面回测 / 纸面下单」，可以在这里根据 paper_trading 切换逻辑。
    # 目前先不区分，统一走真实 client，但 has_long_signal 恒为 False，所以不会真正下单。

    for symbol in symbols:
        try:
            maybe_trade_symbol(client, symbol, enable_trading, order_usdt)
        except Exception as e:
            print(f"❌ 处理 {symbol} 时出错: {e}")
            traceback.print_exc()
            msg_lines.append(f"{symbol}: 失败 - {e}")
        else:
            msg_lines.append(f"{symbol}: 成功（本次无自动下单逻辑，仅检查行情）")

    summary = "本次运行结果：\n" + "\n".join(msg_lines)
    print(summary)

    # 有配置企业微信就推一条汇总
    if os.getenv("WECHAT_WEBHOOK", "").strip():
        try:
            wecom_notify(summary)
        except Exception:
            traceback.print_exc()


def main():
    try:
        run_bot()
    except Exception as e:
        # 兜底异常处理 + 推送
        err_msg = f"run-bot 发生异常: {e}\n\n{traceback.format_exc()[:1500]}"
        print(err_msg)
        if os.getenv("WECHAT_WEBHOOK", "").strip():
            try:
                wecom_notify(err_msg)
            except Exception:
                traceback.print_exc()
        # 抛出去让 GitHub Actions 标成 failed
        raise


if __name__ == "__main__":
    main()
