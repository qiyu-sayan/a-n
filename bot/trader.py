import ccxt
from enum import Enum
from dataclasses import dataclass
from typing import Optional, Any


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"


class PositionSide(str, Enum):
    LONG = "long"
    SHORT = "short"


@dataclass
class OrderRequest:
    symbol: str
    side: Side
    amount: float
    price: Optional[float] = None
    reduce_only: bool = False
    position_side: Optional[PositionSide] = None
    leverage: Optional[int] = None
    params: Optional[dict] = None


class Trader:
    def __init__(self, env: str, spot_ex: ccxt.Exchange, fut_ex: ccxt.Exchange):
        self.env = env
        self.spot = spot_ex
        self.fut = fut_ex

    # 兼容旧调用：trader.futures
    @property
    def futures(self):
        return self.fut

    def place_order(self, req: OrderRequest, market_type="futures") -> Any:
        ex = self.fut if market_type == "futures" else self.spot

        params = req.params.copy() if req.params else {}

        # 合约下单默认参数
        if market_type == "futures":
            params.setdefault("tdMode", "cross")

            if req.position_side:
                params["posSide"] = req.position_side.value

            params["reduceOnly"] = req.reduce_only

        # 调整杠杆
        if req.leverage and market_type == "futures":
            try:
                ex.set_leverage(req.leverage, req.symbol)
            except Exception as e:
                print(f"[Trader] set_leverage 失败 {req.symbol}: {e}")

        try:
            if req.price is None:
                order = ex.create_order(
                    symbol=req.symbol,
                    type="market",
                    side=req.side.value,
                    amount=req.amount,
                    params=params,
                )
            else:
                order = ex.create_order(
                    symbol=req.symbol,
                    type="limit",
                    side=req.side.value,
                    price=req.price,
                    amount=req.amount,
                    params=params,
                )
            print(f"[Trader] 下单成功: {order}")
            return order

        except Exception as e:
            print(f"[Trader] 下单失败 {req.symbol}: {e}")
            return None
