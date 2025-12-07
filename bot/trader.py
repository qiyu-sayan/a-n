from __future__ import annotations

from dataclasses import dataclass, asdict
from enum import Enum
from typing import Optional, Dict, Any

import ccxt


class Env(str, Enum):
    TEST = "test"   # 用 OKX 模拟盘（sandbox）
    LIVE = "live"   # 用 OKX 实盘


class MarketType(str, Enum):
    SPOT = "spot"
    FUTURES = "futures"   # 这里指永续/合约（swap）


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"


class PositionSide(str, Enum):
    LONG = "long"
    SHORT = "short"


@dataclass
class OrderRequest:
    """
    机器人上层只需要构造这个对象丢给 Trader 下单。
    """
    env: Env
    market: MarketType
    symbol: str
    side: Side
    amount: float
    price: Optional[float] = None                # None = 市价单
    leverage: Optional[int] = None               # 合约用
    position_side: Optional[PositionSide] = None # 合约用（多/空），目前主要用来记录方向
    reduce_only: bool = False                    # 合约平仓用
    reason: str = ""                             # 人类可读原因，方便推送 / 日志


@dataclass
class OrderResult:
    success: bool
    env: Env
    market: MarketType
    symbol: str
    side: Side
    amount: float
    price: Optional[float]
    leverage: Optional[int]
    position_side: Optional[PositionSide]
    order_id: Optional[str]
    raw: Dict[str, Any]
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class Trader:
    """
    统一的 OKX 交易引擎（新版）。

    当前版本的使用方式（和 main.py 一致）：

        spot_ex, fut_ex = _create_exchanges(env)
        trader = Trader(env, spot_ex, fut_ex)

    - 只针对 OKX，一个 Trader 对应一个环境（TEST / LIVE）
    - 现货 / 合约共用一套 API key（OKX 统一账户）
    """

    def __init__(
        self,
        env: Env,
        spot_ex: ccxt.Exchange,
        futures_ex: ccxt.Exchange,
    ) -> None:
        self.env = env
        self.spot = spot_ex
        self.futures = futures_ex

        # 容忍不同 ccxt 版本下 OKX 的 id 写法
        okx_ids = {"okx", "okex", "okex5"}

        for ex, name in ((self.spot, "现货"), (self.futures, "合约")):
            ex_id = getattr(ex, "id", "") or ""
            if ex_id and ex_id.lower() not in okx_ids:
                raise ValueError(
                    f"当前 Trader 仅支持 OKX，{name}实际为: {ex_id!r}"
                )

    # ---------- 下单封装 ----------

    def _create_order(
        self,
        client: ccxt.Exchange,
        req: OrderRequest,
        is_futures: bool,
    ) -> Dict[str, Any]:
        """
        统一的下单逻辑，spot / futures 用不同参数。
        """
        order_type = "market" if req.price is None else "limit"
        params: Dict[str, Any] = {}

        if is_futures:
            # 统一使用全仓保证金
            params["tdMode"] = "cross"

            # 单向持仓模式下，不强制传 posSide，由 OKX 自己判断开多/开空/平仓
            if req.reduce_only:
                params["reduceOnly"] = True

            # 设置杠杆（失败就忽略，用默认杠杆）
            if req.leverage is not None:
                try:
                    client.set_leverage(
                        req.leverage,
                        req.symbol,
                        params={"mgnMode": "cross"},
                    )
                except Exception:
                    pass

        return client.create_order(
            symbol=req.symbol,
            type=order_type,
            side=req.side.value,
            amount=req.amount,
            price=None if order_type == "market" else req.price,
            params=params,
        )

    def place_spot_order(self, req: OrderRequest) -> Dict[str, Any]:
        if req.market != MarketType.SPOT:
            raise ValueError("place_spot_order 只支持现货订单 (market=SPOT)")
        return self._create_order(self.spot, req, is_futures=False)

    def place_futures_order(self, req: OrderRequest) -> Dict[str, Any]:
        if req.market != MarketType.FUTURES:
            raise ValueError("place_futures_order 只支持合约订单 (market=FUTURES)")
        return self._create_order(self.futures, req, is_futures=True)

    # 兼容一个通用入口（如果你后面想用的话）
    def place_order(self, req: OrderRequest) -> Dict[str, Any]:
        if req.market == MarketType.SPOT:
            return self.place_spot_order(req)
        else:
            return self.place_futures_order(req)

    # ---------- 给企业微信用的精简文本（目前 main.py 未使用，先保留） ----------

    @staticmethod
    def format_wecom_message(
        req: OrderRequest,
        res: OrderResult,
    ) -> str:
        env_tag = "DEMO" if req.env == Env.TEST else "LIVE"
        market_tag = "现货" if req.market == MarketType.SPOT else "合约"

        if req.market == MarketType.SPOT:
            pos_desc = "现货"
        else:
            if req.position_side == PositionSide.LONG:
                pos_desc = "多头"
            elif req.position_side == PositionSide.SHORT:
                pos_desc = "空头"
            else:
                pos_desc = "合约"

        status = "✅ 成功" if res.success else "❌ 失败"

        lines = [
            f"[{env_tag}] [{market_tag}] {status}",
            f"品种: {req.symbol}",
            f"方向: {pos_desc} / {req.side.value}",
            f"数量: {req.amount}",
        ]

        if req.price is not None:
            lines.append(f"价格: {req.price}")
        else:
            lines.append("价格: 市价单")

        if req.leverage is not None:
            lines.append(f"杠杆: {req.leverage}x")

        if req.reason:
            lines.append(f"原因: {req.reason}")

        if res.order_id:
            lines.append(f"订单ID: {res.order_id}")

        if not res.success and res.error:
            lines.append(f"错误: {res.error}")

        return "\n".join(lines)
