# bot/trader.py
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional, Tuple, Dict, Any

import ccxt


class MarketType(str, Enum):
    SPOT = "spot"
    FUTURES = "futures"


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"


class PositionSide(str, Enum):
    LONG = "long"
    SHORT = "short"


@dataclass
class OrderRequest:
    env: str
    market: MarketType
    symbol: str
    side: Side
    amount: float
    leverage: Optional[int] = None
    price: Optional[float] = None
    position_side: Optional[PositionSide] = None
    reduce_only: bool = False
    reason: str = ""


class Trader:
    def __init__(self, env: str, spot_ex: ccxt.Exchange, fut_ex: ccxt.Exchange):
        self.env = env
        self.spot = spot_ex
        self.fut = fut_ex

    # 兼容旧代码：trader.futures
    @property
    def futures(self):
        return self.fut

    # ------------------------
    # 下单
    # ------------------------
    def place_order(self, req: OrderRequest) -> Tuple[bool, Dict[str, Any]]:
        try:
            ex = self.fut if req.market == MarketType.FUTURES else self.spot

            order_type = "market" if req.price is None else "limit"
            params: Dict[str, Any] = {}

            # ===== 合约下单参数 =====
            if req.market == MarketType.FUTURES:
                # 全仓模式
                params["tdMode"] = "cross"

                # 双向持仓 posSide
                if req.position_side:
                    params["posSide"] = req.position_side.value

                # reduce-only
                if req.reduce_only:
                    params["reduceOnly"] = True

                # 杠杆
                if req.leverage:
                    try:
                        ex.set_leverage(
                            req.leverage,
                            req.symbol,
                            params={"mgnMode": "cross"},
                        )
                    except Exception:
                        # 杠杆设置失败不致命，打印即可
                        pass

            resp = ex.create_order(
                symbol=req.symbol,
                type=order_type,
                side=req.side.value,
                amount=req.amount,
                price=req.price,
                params=params,
            )

            return True, {"order_id": resp.get("id", ""), "raw": resp}

        except Exception as e:
            return False, {"error": str(e)}

    # ------------------------
    # 预留其他功能
    # ------------------------
    # 比如之后可以加：获取持仓、平仓、撤单等
