# bot/trader.py
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
    price: Optional[float]                 # None = 市价单
    leverage: Optional[int] = None        # 合约用
    position_side: Optional[PositionSide] = None  # 合约用（多/空）
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
    统一的 OKX 交易引擎。

    - Env.TEST 使用 OKX 模拟盘（sandbox），读 OKX_PAPER_* 这一套 key
    - Env.LIVE 使用 OKX 实盘，读 OKX_LIVE_* 这一套 key

    OKX 是统一账户，所以 demo/live 各只需要 1 组 key，
    现货和合约可以共用同一组 key。
    """

    def __init__(
        self,
        exchange_id: str,
        paper_keys: Dict[str, str],
        live_keys: Dict[str, str],
    ) -> None:
        if exchange_id != "okx":
            # 目前专门为 OKX 写的，后面若要扩展再说
            raise ValueError("当前 Trader 仅支持 exchange_id='okx'")

        self.exchange_id = exchange_id

        self._paper = self._create_client(paper_keys, Env.TEST)
        self._live = self._create_client(live_keys, Env.LIVE)

    # ---------- 内部：创建 ccxt client ----------

    def _create_client(self, keys: Dict[str, str], env: Env):
        api_key = keys.get("apiKey")
        secret = keys.get("secret")
        password = keys.get("password")

        if not api_key or not secret or not password:
            # 没配完整就返回 None，上层会优雅失败
            return None

        exchange_class = getattr(ccxt, self.exchange_id)
        exchange = exchange_class(
            {
                "apiKey": api_key,
                "secret": secret,
                "password": password,   # OKX 必须要 password/passphrase
                "enableRateLimit": True,
            }
        )

        # 模拟盘：启用 sandbox 模式
        if env == Env.TEST:
            try:
                exchange.set_sandbox_mode(True)
            except Exception:
                # 如果版本不支持 sandbox，就忽略（到时下单会直接报错，我们再看具体问题）
                pass

        return exchange

    def _choose_client(self, env: Env):
        if env == Env.TEST:
            return self._paper
        else:
            return self._live

    # ---------- 下单主入口 ----------

    def place_order(self, req: OrderRequest) -> OrderResult:
        client = self._choose_client(req.env)

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
                error="对应环境没有配置完整的 OKX API（apiKey/secret/password）",
            )

        order_type = "market" if req.price is None else "limit"

        params: Dict[str, Any] = {}

        # 合约相关参数
        if req.market == MarketType.FUTURES:
            # 保证金模式：统一用全仓 cross
            params["tdMode"] = "cross"

            # 单向持仓模式下，不传 posSide，直接用 side=buy/sell 控制方向
            # buy：开多或平空；sell：开空或平多，由 OKX 自己判断

            if req.reduce_only:
                params["reduceOnly"] = True

            # 设置杠杆（不区分多空）
            if req.leverage is not None:
                try:
                    client.set_leverage(
                        req.leverage,
                        req.symbol,
                        params={"mgnMode": "cross"},
                    )
                except Exception:
                    # 失败就用默认杠杆
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
