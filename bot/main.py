# bot/main.py
"""
简单版自动交易主程序：
- 从 config/params.json 读取交易配置（symbols / interval 等）
- 定时拉取 Binance 永续合约 K 线
- 用非常简单的均线策略生成信号（之后可以换成你自己的）
- 下单前用 trade_utils 里的风控做检查
- 下单后记录 trade_log.csv，方便训练 / 复盘

依赖：
  pip install python-binance

环境变量（由 GitHub Actions 注入）：
  BINANCE_KEY
  BINANCE_SECRET
  BINANCE_TESTNET   -> "1"表示用 testnet
  ENABLE_TRADING    -> "true" 才真的下单
  ORDER_USDT        -> 每笔名义金额，例如 10
  PAPER             -> "true" 只纸面交易，不打到交易所

  RISK_LIMIT_USDT
  MAX_OPEN_TRADES
  STOP_LOSS_PCT
  TAKE_PROFIT_PCT
  SLIPPAGE_BPS
"""

import os
import time
import json
import logging
from typing import Dict, Any, List

from binance.client import Client
from binance.enums import SIDE_BUY, SIDE_SELL, ORDER_TYPE_MARKET

from bot.trade_utils import (
    enforce_risk_limits,
    price_with_slippage,
    oco_levels,
    append_trade_log,
    safe_json,
)

# ----------------- 日志配置 -----------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

logger = logging.getLogger(__name__)

# ----------------- 配置与客户端 -----------------


def load_params(path: str = "config/params.json") -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    # 一些默认值兜底
    cfg.setdefault("symbols", ["BTCUSDT"])
    cfg.setdefault("interval", "1m")
    cfg.setdefault("lookback", 200)
    cfg.setdefault("fast_ma", 7)
    cfg.setdefault("slow_ma", 25)
    cfg.setdefault("risk", {})
    return cfg


def create_client() -> Client:
    key = os.getenv("BINANCE_KEY", "")
    secret = os.getenv("BINANCE_SECRET", "")
    testnet_flag = os.getenv("BINANCE_TESTNET", "0").lower() in ("1", "true", "yes")

    client = Client(key, secret)

    if testnet_flag:
        # Binance 合约 testnet 端点
        client.FUTURES_URL = "https://testnet.binancefuture.com/fapi"
        logger.info("Using Binance FUTURES TESTNET endpoint")
    else:
        logger.info("Using Binance FUTURES LIVE endpoint")

    return client


def is_trading_enabled() -> bool:
    return os.getenv("ENABLE_TRADING", "false").lower() == "true"


def is_paper_trading() -> bool:
    return os.getenv("PAPER", "true").lower() == "true"


def get_order_usdt() -> float:
    try:
        return float(os.getenv("ORDER_USDT", "10"))
    except Exception:
        return 10.0


# ----------------- 数据 / 策略 -----------------


def fetch_klines(
    client: Client, symbol: str, interval: str, limit: int = 200
) -> List[Dict[str, Any]]:
    """拉取 K 线，返回 list[dict]，只保留 open_time, open, high, low, close, volume"""
    raw = client.futures_klines(symbol=symbol, interval=interval, limit=limit)
    bars = []
    for k in raw:
        bars.append(
            {
                "open_time": k[0],
                "open": float(k[1]),
                "high": float(k[2]),
                "low": float(k[3]),
                "close": float(k[4]),
                "volume": float(k[5]),
            }
        )
    return bars


def ma(values: List[float], window: int) -> float:
    if len(values) < window:
        return sum(values) / max(len(values), 1)
    return sum(values[-window:]) / window


def generate_signal(
    closes: List[float], fast: int, slow: int
) -> str:
    """非常简单的均线交叉：fast>slow 买入；fast<slow 卖出；其余不动"""
    if len(closes) < slow:
        return "hold"
    fast_ma = ma(closes, fast)
    slow_ma = ma(closes, slow)
    logger.info("fast_ma=%.4f slow_ma=%.4f", fast_ma, slow_ma)
    if fast_ma > slow_ma * 1.001:
        return "long"
    elif fast_ma < slow_ma * 0.999:
        return "short"
    else:
        return "hold"


# ----------------- 持仓与下单 -----------------


def get_open_positions_count(client: Client, symbol: str) -> int:
    """简化版：看该 symbol 是否有正/负仓位"""
    positions = client.futures_position_information(symbol=symbol)
    cnt = 0
    for p in positions:
        pos_amt = float(p.get("positionAmt", "0"))
        if abs(pos_amt) > 1e-6:
            cnt += 1
    return cnt


def calc_order_qty(usdt: float, price: float, step_size: float = 0.001) -> float:
    """按名义金额计算数量，并粗略对齐到 step_size"""
    if price <= 0:
        return 0.0
    qty = usdt / price
    # 向下取整到交易所步长（这里先简单用 3 位小数）
    qty = int(qty / step_size) * step_size
    return round(qty, 6)


def place_futures_market_order(
    client: Client,
    symbol: str,
    side: str,
    qty: float,
) -> Dict[str, Any]:
    """真正打到 Binance 的订单。只处理市价单。"""
    resp = client.futures_create_order(
        symbol=symbol,
        side=side,
        type=ORDER_TYPE_MARKET,
        quantity=qty,
    )
    return resp


# ----------------- 主流程 -----------------


def trade_symbol(client: Client, cfg: Dict[str, Any], symbol: str):
    interval = cfg["interval"]
    lookback = int(cfg.get("lookback", 200))
    fast = int(cfg.get("fast_ma", 7))
    slow = int(cfg.get("slow_ma", 25))

    logger.info("=== Trading %s (interval=%s) ===", symbol, interval)

    bars = fetch_klines(client, symbol, interval, limit=lookback)
    closes = [b["close"] for b in bars]
    last_price = closes[-1]

    signal = generate_signal(closes, fast, slow)
    logger.info("Signal for %s: %s (last_price=%.4f)", symbol, signal, last_price)

    if signal == "hold":
        return

    order_usdt = get_order_usdt()
    qty = calc_order_qty(order_usdt, last_price)
    if qty <= 0:
        logger.warning("qty <= 0, skip")
        return

    side = SIDE_BUY if signal == "long" else SIDE_SELL

    # ---- 下单前：风控检查 ----
    open_cnt = get_open_positions_count(client, symbol)
    enforce_risk_limits(symbol, last_price, qty, open_cnt)

    # 轻微滑点：虽然是 MARKET 单，但我们把“理论成交价”调一下，用于日志 / 风控
    adj_price = price_with_slippage(last_price, "buy" if side == SIDE_BUY else "sell")

    live = is_trading_enabled()
    paper = is_paper_trading()

    logger.info(
        "Ready to place order: symbol=%s side=%s qty=%s notional=%.2f live=%s paper=%s",
        symbol,
        side,
        qty,
        adj_price * qty,
        live,
        paper,
    )

    ts = int(time.time())
    resp: Dict[str, Any] = {}

    if live and not paper:
        # 真正下单
        resp = place_futures_market_order(client, symbol, side, qty)
        logger.info("Order response: %s", safe_json(resp))
    else:
        # 纸面交易 / 关闭实盘模式
        logger.info("Dry-run / paper trade, no real order sent")
        resp = {
            "symbol": symbol,
            "side": side,
            "qty": qty,
            "price": adj_price,
            "paper": True,
        }

    # 这里我们只记录开仓，exit_price / pnl 先用 0，等平仓逻辑完善后再补充
    append_trade_log(
        ts=ts,
        symbol=symbol,
        side="LONG" if signal == "long" else "SHORT",
        qty=qty,
        entry=adj_price,
        exit_px=0.0,
        pnl=0.0,
        reason="entry",
    )

    # 计算并打印止盈/止损价（现在只是打印，没有真正挂单）
    tp, sl = oco_levels(adj_price)
    logger.info("TP=%.4f SL=%.4f (未真正挂单，需要再接 Binance OCO / 止损接口)", tp, sl)


def main():
    cfg = load_params()
    client = create_client()

    logger.info("Config: %s", safe_json(cfg))

    symbols = cfg.get("symbols", [])
    if not symbols:
        logger.warning("No symbols configured, exit")
        return

    for sym in symbols:
        try:
            trade_symbol(client, cfg, sym)
        except Exception as e:
            logger.exception("Error trading %s: %s", sym, e)


if __name__ == "__main__":
    main()