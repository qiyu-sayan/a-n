from typing import List
from trader import OrderRequest, Side, PositionSide

# ccxt 标准合约符号
FUTURE_SYMBOLS = [
    "ETH/USDT:USDT",
    "BTC/USDT:USDT",
]

# 最小下单单位（与 symbol 完全一致）
MIN_QTY = {
    "ETH/USDT:USDT": 0.01,
    "BTC/USDT:USDT": 0.001,
}

TARGET_NOTIONAL = 20  # 默认每次下单名义金额（USDT）


class Strategy:
    """
    示例策略：无限趋近“如果是涨趋势→开多；跌趋势→开空”
    """

    def __init__(self, trader):
        self.trader = trader

    def _trend_signal(self, candles):
        """
        示例信号：使用收盘价判断趋势
        """
        if len(candles) < 3:
            return None

        c1 = candles[-3][4]
        c2 = candles[-2][4]
        c3 = candles[-1][4]

        if c3 > c2 > c1:
            return "long"

        if c3 < c2 < c1:
            return "short"

        return None

    def generate_orders(self, symbol: str, candles: list) -> List[OrderRequest]:
        sig = self._trend_signal(candles)
        if not sig:
            return []

        latest_price = candles[-1][4]

        amount = TARGET_NOTIONAL / latest_price
        min_qty = MIN_QTY.get(symbol, 0.0)

        if amount < min_qty:
            print(f"[Strategy] {symbol} 数量过小，自动提升到最小单位 {min_qty}")
            amount = min_qty

        # long or short
        if sig == "long":
            return [
                OrderRequest(
                    symbol=symbol,
                    side=Side.BUY,
                    amount=amount,
                    price=None,
                    position_side=PositionSide.LONG,
                    leverage=3,
                )
            ]

        if sig == "short":
            return [
                OrderRequest(
                    symbol=symbol,
                    side=Side.SELL,
                    amount=amount,
                    price=None,
                    position_side=PositionSide.SHORT,
                    leverage=3,
                )
            ]

        return []
