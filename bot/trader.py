import os
import logging
from dataclasses import dataclass
from typing import Optional

from dotenv import load_dotenv
from binance.spot import Spot as BinanceSpot

load_dotenv()  # 允许从 .env 读取环境变量

@dataclass
class TraderConfig:
    dry_run: bool = True
    testnet: bool = True

class Trader:
    """
    最小下单适配层：
    - dry_run=True：只打印，不下单
    - dry_run=False：用 Binance 下单（默认 Testnet，可切换正式网）
    """
    def __init__(self, cfg: TraderConfig):
        self.cfg = cfg
        self._client: Optional[BinanceSpot] = None

        if not self.cfg.dry_run:
            key = os.getenv("BINANCE_KEY", "")
            sec = os.getenv("BINANCE_SECRET", "")
            if not key or not sec:
                raise RuntimeError("BINANCE_KEY / BINANCE_SECRET 未配置，但 dry_run=False。")

            base_url = "https://testnet.binance.vision" if self.cfg.testnet else None
            self._client = BinanceSpot(key=key, secret=sec, base_url=base_url)
            logging.info(f"Trader: Binance client ready (testnet={self.cfg.testnet}).")
        else:
            logging.info("Trader: DRY_RUN 模式，仅打印信号，不会真实下单。")

    # ---- 下单原语（现货市价单 + OCO 止盈止损） ----
    def market_order(self, symbol: str, side: str, qty: float):
        side = side.upper()
        if self.cfg.dry_run:
            logging.info(f"[DRY] MARKET {side} {symbol} qty={qty}")
            return {"dry_run": True, "side": side, "symbol": symbol, "qty": qty}

        # Binance 现货市价单
        logging.info(f"MARKET {side} {symbol} qty={qty}")
        return self._client.new_order(symbol=symbol, side=side, type="MARKET", quantity=str(qty))

    def oco(self, symbol: str, qty: float, take_profit_price: float, stop_price: float, stop_limit_price: float):
        """创建 OCO 组合（止盈+止损）。OCO 仅支持 SELL。用于持有多头时一次性挂两个单。"""
        if self.cfg.dry_run:
            logging.info(f"[DRY] OCO SELL {symbol} qty={qty} tp={take_profit_price} sl={stop_price}/{stop_limit_price}")
            return {"dry_run": True}

        logging.info(f"OCO SELL {symbol} qty={qty} tp={take_profit_price} sl={stop_price}/{stop_limit_price}")
        return self._client.new_oco_order(
            symbol=symbol,
            side="SELL",
            quantity=str(qty),
            price=str(take_profit_price),
            stopPrice=str(stop_price),
            stopLimitPrice=str(stop_limit_price),
            stopLimitTimeInForce="GTC",
        )