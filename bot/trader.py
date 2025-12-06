# bot/trader.py
from __future__ import annotations

from dataclasses import dataclass, asdict
from enum import Enum
from typing import Optional, Dict, Any

import ccxt


class Env(str, Enum):
    TEST = "test"   # 这里的 TEST = Binance Demo Trading
    LIVE = "live"   # 将来接实盘用


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
    """
    上层只需要构造这个对象丢给 Trader
    """
    env: Env
    market: MarketType
    symbol: str
    side: Side
    amount: float
    price: Optional[float]                 # None = 市价
    leverage: Optional[int] = None        # 合约用
    position_side: Optional[PositionSide] = None  # 合约用
    reduce_only: bool = False             # 合约平仓用
    reason: str = ""                      # 人类可读原因，方便推送


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
    统一的交易引擎：

    - Env.TEST  = Binance Demo Trading（现货 + 合约用同一套 demo key）
    - Env.LIVE  = 将来接正式实盘

    你现在只需要一套 demo API，就可以同时下现货和合约单。
    """

    def __init__(
        self,
        exchange_id: str,
        demo_keys: Dict[str, str],
        live_spot_keys: Optional[Dict[str, str]] = None,
        live_futures_keys: Optional[Dict[str, str]] = None,
    ) -> None:
        self.exchange_id = exchange_id

        live_spot_keys = live_spot_keys or {}
        live_futures_keys = live_futures_keys or {}

        # Demo：现货 + 合约共用一套 key
        self._spot_demo = self._create_client(demo_keys, MarketType.SPOT, Env.TEST)
        self._futures_demo = self._create_client(demo_keys, MarketType.FUTURES, Env.TEST)

        # Live：为以后接实盘预留
        self._spot_live = self._create_client(live_spot_keys, MarketType.SPOT, Env.LIVE)
        self._futures_live = self._create_client(live_futures_keys, MarketType.FUTURES, Env.LIVE)

    # ---------- 内部：创建 client ----------

    def _create_client(
        self,
        keys: Dict[str, str],
        market_type: MarketType,
        env: Env,
    ):
        if not keys or not keys.get("apiKey"):
            return None

        exchange_class = getattr(ccxt, self.exchange_id)
        exchange = exchange_class(
            {
                "apiKey": keys.get("apiKey"),
                "secret": keys.get("secret"),
                "enableRateLimit": True,
            }
        )

        # === Binance Demo Trading 特殊处理 ===
        # 官方文档：Demo Trading 提供独立的 Spot / Futures 域名 :contentReference[oaicite:0]{index=0}
        if self.exchange_id == "binance" and env == Env.TEST:
            if market_type == MarketType.SPOT:
                # Spot Demo base: https://demo-api.binance.com
                urls = exchange.urls
                urls["api"] = "https://demo-api.binance.com/api"
                urls["sapi"] = "https://demo-api.binance.com/sapi"
                exchange.urls = urls
                exchange.options["defaultType"] = "spot"
            else:
                # Futures Demo base: https://demo-fapi.binance.com
                urls = exchange.urls
                urls["fapi"] = "https://demo-fapi.binance.com/fapi"
                urls["fapiPublic"] = "https://demo-fapi.binance.com/fapi"
                urls["fapiPrivate"] = "https://demo-fapi.binance.com/fapi"
                exchange.urls = urls
                exchange.options["defaultType"] = "future"

                # 尝试开启 hedge 模式（允许 LONG/SHORT）
                try:
                    exchange.set_position_mode(True)
                except Exception:
                    pass

            return exchange

        # === 其它情况（包括将来实盘 or 其它交易所） ===
        if market_type == MarketType.SPOT:
            exchange.options["defaultType"] = "spot"
        else:
            if self.exchange_id == "binance":
                exchange.options["defaultType"] = "future"
            else:
                exchange.options["defaultType"] = "swap"

        return exchange

    def _choose_client(self, env: Env, market: MarketType):
        if env == Env.TEST:
            return self._spot_demo if market == MarketType.SPOT else self._futures_demo
        else:
            return self._spot_live if market == MarketType.SPOT else self._futures_live

    # ---------- 下单主入口 ----------

    def place_order(self, req: OrderRequest) -> OrderResult:
        client = self._choose_client(req.env, req.market)

        if client is None:
            return OrderResult(
                success=False,
                env=req.env,
                market=req.market,
                symbol=req.symbol,
                side=req.side,
                amount=req.amount,
                price=req.price,
                leverage=req.leverage,
                position_side=req.position_side,
                order_id=None,
                raw={},
                error="对应环境/市场没有配置 API Key（client 为 None）",
            )

        order_type = "market" if req.price is None else "limit"

        params: Dict[str, Any] = {}

        if req.market == MarketType.FUTURES:
            if req.position_side is not None:
                params["positionSide"] = req.position_side.name.upper()
            if req.reduce_only:
                params["reduceOnly"] = True
            if req.leverage is not None:
                try:
                    client.set_leverage(req.leverage, req.symbol)
                except Exception:
                    pass

        try:
            order = client.create_order(
                symbol=req.symbol,
                type=order_type,
                side=req.side.value,
                amount=req.amount,
                price=None if order_type == "market" else req.price,
                params=params,
            )
            order_id = order.get("id") or order.get("orderId")
            return OrderResult(
                success=True,
                env=req.env,
                market=req.market,
                symbol=req.symbol,
                side=req.side,
                amount=req.amount,
                price=req.price,
                leverage=req.leverage,
                position_side=req.position_side,
                order_id=order_id,
                raw=order,
                error=None,
            )
        except Exception as e:
            return OrderResult(
                success=False,
                env=req.env,
                market=req.market,
                symbol=req.symbol,
                side=req.side,
                amount=req.amount,
                price=req.price,
                leverage=req.leverage,
                position_side=req.position_side,
                order_id=None,
                raw={},
                error=str(e),
            )

    # ---------- 给企业微信用的精简文本 ----------

    @staticmethod
    def format_wecom_message(req: OrderRequest, res: OrderResult) -> str:
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
