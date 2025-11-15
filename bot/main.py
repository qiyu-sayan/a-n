# bot/main.py
"""
MACD + RSI 组合策略自动交易机器人

环境变量（通过 GitHub Secrets 传入）：
- BINANCE_KEY        : 币安 API Key
- BINANCE_SECRET     : 币安 Secret Key
- BINANCE_TESTNET    : "true"/"false"   是否使用测试网
- ENABLE_TRADING     : "true"/"false"   是否允许真实下单
- PAPER              : "true"/"false"   是否仅模拟（不触碰交易所）
- ORDER_USDT         : 每笔交易使用的 USDT 金额，例如 "10"
- SYMBOLS            : 要交易的交易对，逗号分隔，例如 "BTCUSDT,ETHUSDT"

策略概要：
- 只做多（现货）
- MACD 金叉 + RSI 在 [50, 70) 时买入
- 买入后立刻挂止损卖单（默认 0.5% 止损，可在 STOP_LOSS_PCT 调整）
"""

import os
import sys
import math
import logging
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from binance.client import Client
from binance.exceptions import BinanceAPIException, BinanceRequestException

# ===== 基本配置 =====
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
STOP_LOSS_PCT = float(os.getenv("STOP_LOSS_PCT", "0.005"))  # 0.5% 止损
INTERVAL = os.getenv("INTERVAL", "15m")  # 默认 15 分钟 K 线

API_KEY = os.getenv("BINANCE_KEY")
API_SECRET = os.getenv("BINANCE_SECRET")
TESTNET = os.getenv("BINANCE_TESTNET", "true").lower() == "true"
ENABLE_TRADING = os.getenv("ENABLE_TRADING", "false").lower() == "true"
PAPER = os.getenv("PAPER", "true").lower() == "true"
ORDER_USDT = float(os.getenv("ORDER_USDT", "10"))

SYMBOLS_ENV = os.getenv("SYMBOLS", "BTCUSDT")
SYMBOLS = [s.strip().upper() for s in SYMBOLS_ENV.split(",") if s.strip()]

# ===== 日志设置 =====
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)


# ===== 币安客户端封装 =====
def create_client() -> Client:
    if not API_KEY or not API_SECRET:
        raise RuntimeError("BINANCE_KEY / BINANCE_SECRET 未设置")

    client = Client(API_KEY, API_SECRET)

    if TESTNET:
        # python-binance 测试网地址
        client.API_URL = "https://testnet.binance.vision/api"
        logger.info("使用 Binance TESTNET")
    else:
        logger.info("使用 Binance MAINNET")

    return client


def get_symbol_precision(client: Client, symbol: str):
    """获取交易数量和价格精度，用于正确下单"""
    info = client.get_symbol_info(symbol)
    if info is None:
        raise RuntimeError(f"无法获取交易对信息: {symbol}")

    qty_precision = 8
    price_precision = 8

    for f in info["filters"]:
        if f["filterType"] == "LOT_SIZE":
            step = float(f["stepSize"])
            qty_precision = max(0, int(round(-math.log10(step))))
        if f["filterType"] == "PRICE_FILTER":
            tick = float(f["tickSize"])
            price_precision = max(0, int(round(-math.log10(tick))))

    return qty_precision, price_precision


def fetch_klines(client: Client, symbol: str, interval: str, limit: int = 200) -> pd.DataFrame:
    """拉取 K 线数据并转为 DataFrame"""
    raw = client.get_klines(symbol=symbol, interval=interval, limit=limit)
    cols = [
        "open_time", "open", "high", "low", "close", "volume",
        "close_time", "quote_asset_volume", "number_of_trades",
        "taker_buy_base_asset_volume", "taker_buy_quote_asset_volume", "ignore",
    ]
    df = pd.DataFrame(raw, columns=cols)
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    df["close_time"] = pd.to_datetime(df["close_time"], unit="ms", utc=True)
    df["open"] = df["open"].astype(float)
    df["high"] = df["high"].astype(float)
    df["low"] = df["low"].astype(float)
    df["close"] = df["close"].astype(float)
    df["volume"] = df["volume"].astype(float)
    return df


# ===== 技术指标计算 =====
def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    close = df["close"]

    # MACD
    ema_fast = close.ewm(span=12, adjust=False).mean()
    ema_slow = close.ewm(span=26, adjust=False).mean()
    macd = ema_fast - ema_slow
    signal = macd.ewm(span=9, adjust=False).mean()

    # RSI
    delta = close.diff()
    gain = (delta.where(delta > 0, 0.0)).rolling(window=14).mean()
    loss = (-delta.where(delta < 0, 0.0)).rolling(window=14).mean()
    rs = gain / loss
    rsi = 100 - (100 / (1 + rs))

    df["macd"] = macd
    df["signal"] = signal
    df["rsi"] = rsi

    return df


# ===== 策略逻辑：MACD + RSI 组合 =====
def generate_signal(df: pd.DataFrame):
    """返回 'BUY' / 'FLAT'"""
    if len(df) < 35:
        return "FLAT"

    last = df.iloc[-1]
    prev = df.iloc[-2]

    macd_now = last["macd"]
    signal_now = last["signal"]
    macd_prev = prev["macd"]
    signal_prev = prev["signal"]
    rsi_now = last["rsi"]

    if any(pd.isna([macd_now, signal_now, macd_prev, signal_prev, rsi_now])):
        return "FLAT"

    # MACD 金叉
    bull_cross = macd_prev <= signal_prev and macd_now > signal_now
    # RSI 过滤：50~70 之间，偏多但不过热
    rsi_ok = 50 <= rsi_now < 70

    if bull_cross and rsi_ok:
        return "BUY"

    return "FLAT"


# ===== 交易执行相关 =====
def has_open_orders(client: Client, symbol: str) -> bool:
    orders = client.get_open_orders(symbol=symbol)
    return len(orders) > 0


def get_last_price(client: Client, symbol: str) -> float:
    ticker = client.get_symbol_ticker(symbol=symbol)
    return float(ticker["price"])


def calc_quantity(usdt_amount: float, price: float, qty_precision: int) -> float:
    raw_qty = usdt_amount / price
    fmt = "{:0." + str(qty_precision) + "f}"
    return float(fmt.format(raw_qty))


def place_order_with_sl(
    client: Client,
    symbol: str,
    side: str,
    usdt_amount: float,
    stop_loss_pct: float,
    qty_precision: int,
    price_precision: int,
):
    """市价开仓 + 挂止损卖单"""
    price = get_last_price(client, symbol)
    qty = calc_quantity(usdt_amount, price, qty_precision)

    if qty <= 0:
        logger.warning(f"{symbol}: 计算得到的下单数量 <= 0，跳过。")
        return

    logger.info(f"{symbol}: 计划 {side}，价格≈{price}, 数量={qty}")

    if PAPER or not ENABLE_TRADING:
        logger.info(f"{symbol}: PAPER 模式 / 关闭真实交易，仅打印模拟下单。")
        return

    try:
        if side.upper() == "BUY":
            order = client.order_market_buy(symbol=symbol, quantity=qty)
        else:
            order = client.order_market_sell(symbol=symbol, quantity=qty)

        logger.info(f"{symbol}: 市价单已提交: {order['orderId']}")
    except (BinanceAPIException, BinanceRequestException) as e:
        logger.error(f"{symbol}: 市价单下单失败: {e}")
        return

    # 止损价
    stop_price = price * (1 - stop_loss_pct)
    fmt = "{:0." + str(price_precision) + "f}"
    stop_price_str = fmt.format(stop_price)

    try:
        sl_order = client.create_order(
            symbol=symbol,
            side=Client.SIDE_SELL,
            type=Client.ORDER_TYPE_STOP_LOSS_LIMIT,
            timeInForce=Client.TIME_IN_FORCE_GTC,
            quantity=qty,
            price=stop_price_str,
            stopPrice=stop_price_str,
        )
        logger.info(f"{symbol}: 止损单已挂单: {sl_order['orderId']} @ {stop_price_str}")
    except (BinanceAPIException, BinanceRequestException) as e:
        logger.error(f"{symbol}: 止损单下单失败: {e}")


# ===== 主流程 =====
def run_for_symbol(client: Client, symbol: str):
    logger.info(f"===== 处理交易对 {symbol} =====")

    try:
        qty_precision, price_precision = get_symbol_precision(client, symbol)
        df = fetch_klines(client, symbol, INTERVAL)
        df = add_indicators(df)
        signal = generate_signal(df)
        last = df.iloc[-1]
        logger.info(
            f"{symbol}: 收盘价={last['close']:.4f}, MACD={last['macd']:.5f}, "
            f"Signal={last['signal']:.5f}, RSI={last['rsi']:.2f}, 信号={signal}"
        )

        if has_open_orders(client, symbol):
            logger.info(f"{symbol}: 有未成交/挂单订单，跳过本次信号。")
            return

        if signal == "BUY":
            place_order_with_sl(
                client,
                symbol,
                "BUY",
                ORDER_USDT,
                STOP_LOSS_PCT,
                qty_precision,
                price_precision,
            )
        else:
            logger.info(f"{symbol}: 当前信号为 {signal}，不交易。")

    except (BinanceAPIException, BinanceRequestException) as e:
        logger.error(f"{symbol}: Binance API 错误: {e}")
    except Exception as e:
        logger.exception(f"{symbol}: 未预期错误: {e}")


def main():
    logger.info("=== 自动交易机器人启动（MACD + RSI）===")
    logger.info(
        f"TESTNET={TESTNET}, ENABLE_TRADING={ENABLE_TRADING}, "
        f"PAPER={PAPER}, ORDER_USDT={ORDER_USDT}, SYMBOLS={SYMBOLS}"
    )

    client = create_client()

    for symbol in SYMBOLS:
        run_for_symbol(client, symbol)

    logger.info("=== 本次运行结束 ===")


if __name__ == "__main__":
    main()
