# bot/virtual_pnl.py
from __future__ import annotations

import csv
import os
from dataclasses import dataclass
from typing import Dict, Optional

from bot.trader import OrderRequest, PositionSide
from datetime import datetime


def _now_ts() -> str:
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")


@dataclass
class VirtualPosition:
    """虚拟持仓，用于计算胜率，不依赖交易所持仓。"""
    symbol: str
    side: PositionSide
    qty: float
    entry_price: float
    entry_time: str
    reason_open: str = ""


@dataclass
class ClosedTrade:
    """一笔完成的虚拟交易（用于胜率统计）。"""
    symbol: str
    side: PositionSide          # 被平掉的方向（long / short）
    qty: float
    entry_price: float
    exit_price: float
    entry_time: str
    exit_time: str
    pnl: float                  # 以 USDT 计价
    reason_open: str
    reason_close: str


class VirtualPositionManager:
    """
    虚拟开平仓管理器：

    - 不依赖 OKX 模拟盘的持仓 / 平仓能力
    - 每个 symbol 只维护一笔净仓位（long 或 short）
    - 方向相反的订单会触发虚拟平仓，并记录盈亏
    """

    def __init__(self, env: str):
        self.env = env
        self.positions: Dict[str, VirtualPosition] = {}
        self.log_path = f"logs/virtual_trades_{env}.csv"

        os.makedirs(os.path.dirname(self.log_path), exist_ok=True)
        if not os.path.exists(self.log_path):
            with open(self.log_path, "w", newline="", encoding="utf8") as f:
                w = csv.writer(f)
                w.writerow(
                    [
                        "symbol",
                        "side",
                        "qty",
                        "entry_price",
                        "exit_price",
                        "entry_time",
                        "exit_time",
                        "pnl",
                        "reason_open",
                        "reason_close",
                    ]
                )

    @staticmethod
    def _calc_pnl(side: PositionSide, entry_price: float, exit_price: float, qty: float) -> float:
        """
        计算一笔交易的盈亏（USDT）：
        - 多单： (exit - entry) * qty
        - 空单： (entry - exit) * qty
        """
        if side == PositionSide.LONG:
            return (exit_price - entry_price) * qty
        else:
            return (entry_price - exit_price) * qty

    def _log_closed_trade(self, trade: ClosedTrade) -> None:
        with open(self.log_path, "a", newline="", encoding="utf8") as f:
            w = csv.writer(f)
            w.writerow(
                [
                    trade.symbol,
                    trade.side.value,
                    trade.qty,
                    trade.entry_price,
                    trade.exit_price,
                    trade.entry_time,
                    trade.exit_time,
                    trade.pnl,
                    trade.reason_open.replace("\n", " | "),
                    trade.reason_close.replace("\n", " | "),
                ]
            )

    def on_order_filled(self, req: OrderRequest, fill_price: float) -> Optional[ClosedTrade]:
        """
        在订单“确认成功”后调用，更新虚拟持仓。

        返回：
            - 如果本次触发了虚拟平仓，返回 ClosedTrade（用于本轮 summary）
            - 否则返回 None
        """
        # 必须有方向信息（合约双向持仓）
        if not req.position_side:
            return None

        symbol = req.symbol
        side = req.position_side
        qty = float(req.amount)
        ts = _now_ts()

        current = self.positions.get(symbol)

        # 1. 当前没仓位 -> 直接开仓
        if current is None:
            self.positions[symbol] = VirtualPosition(
                symbol=symbol,
                side=side,
                qty=qty,
                entry_price=fill_price,
                entry_time=ts,
                reason_open=req.reason or "",
            )
            return None

        # 2. 同方向 -> 加仓，更新平均成本
        if current.side == side:
            new_qty = current.qty + qty
            if new_qty <= 0:
                return None
            current.entry_price = (
                current.entry_price * current.qty + fill_price * qty
            ) / new_qty
            current.qty = new_qty
            # entry_time 和 reason_open 保持最早那一笔
            return None

        # 3. 反方向 -> 先平掉旧仓，再用本次订单开新仓
        pnl = self._calc_pnl(current.side, current.entry_price, fill_price, current.qty)
        closed = ClosedTrade(
            symbol=symbol,
            side=current.side,
            qty=current.qty,
            entry_price=current.entry_price,
            exit_price=fill_price,
            entry_time=current.entry_time,
            exit_time=ts,
            pnl=pnl,
            reason_open=current.reason_open,
            reason_close=req.reason or "",
        )
        self._log_closed_trade(closed)

        # 开新反向仓
        self.positions[symbol] = VirtualPosition(
            symbol=symbol,
            side=side,
            qty=qty,
            entry_price=fill_price,
            entry_time=ts,
            reason_open=req.reason or "",
        )

        return closed
