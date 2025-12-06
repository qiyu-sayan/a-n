# bot/trader.py
"""
统一的交易引擎（现货 + 合约 + test/live）

设计目标：
- 现货、合约、test、live 完全用不同的 API Key，互不干扰
- 每次下单都返回一个结构化结果，方便主程序和企业微信使用
- 主程序只关心“我要下什么单”，不关心具体交易所细节
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from enum import Enum
from typing import Optional, Dict, Any

import ccxt


# ========= 基础枚举 =========

class Env(str, Enum):
    TEST = "test"
    LIVE = "live"


class MarketType(str, Enum):
    SPOT = "spot"
    FUTURES = "futures"  # 统一称 futures，这里指永续/USDT 永续等


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"


class PositionSide(str, Enum):
    LONG = "long"
    SHORT = "short"


# ========= 下单请求 / 结果数据结构 =========

@dataclass
class OrderRequest:
    """
    策略层/主程序 想要下单时，只要构造这个对象丢给 Trader 就行
    """
    env: Env                   # TEST / LIVE
    market: MarketType         # SPOT / FUTURES
    symbol: str                # 交易对，如 "BTC/USDT" 或 "BTC-USDT-SWAP"
    side: Side                 # buy / sell
    amount: float              # 数量（现货：币的数量；合约：张数 or 币数，看你的交易所）
    price: Optional[float]     # None = 市价单，其他 = 限价单
    leverage: Optional[int] = None          # 合约杠杆，现货可以 None
    position_side: Optional[PositionSide] = None  # 合约用：LONG / SHORT；现货用 None
    reduce_only: bool = False              # 合约平仓时用
    reason: str = ""                       # 策略给的人类可读的“下单原因”，方便推送


@dataclass
class OrderResult:
    """
    下单完成后的统一返回结果，方便：
    - 记录日志
    - 生成企业微信文本
    """
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
    raw: Dict[str, Any]                   # 交易所原始返回（可以写入日志）
    error: Optional[str] = None           # 如果失败，这里放错误信息

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ========= Trader 主类 =========

class Trader:
    """
    统一管理：
    - 现货 test / 现货 live
    - 合约 test / 合约 live

    注意：
    - 这里使用 ccxt，exchange_id 可以是 "binance"、"okx" 等
    - test / live 的区别主要通过：
        1) 使用不同的 API Key
        2) 对支持 sandbox 的交易所，给 test 环境启用 set_sandbox_mode(True)
    """

    def __init__(
        self,
        exchange_id: str,
        spot_test_keys: Dict[str, str],
        spot_live_keys: Dict[str, str],
        futures_test_keys: Dict[str, str],
        futures_live_keys: Dict[str, str],
    ) -> None:
        self.exchange_id = exchange_id

        # 现货 client
        self._spot_test = self._create_client(spot_test_keys, market_type=MarketType.SPOT, env=Env.TEST)
        self._spot_live = self._create_client(spot_live_keys, market_type=MarketType.SPOT, env=Env.LIVE)

        # 合约 client
        self._futures_test = self._create_client(futures_test_keys, market_type=MarketType.FUTURES, env=Env.TEST)
        self._futures_live = self._create_client(futures_live_keys, market_type=MarketType.FUTURES, env=Env.LIVE)

    # ----- 内部工具函数 -----

    def _create_client(
        self,
        keys: Dict[str, str],
        market_type: MarketType,
        env: Env,
    ):
        """
        创建 ccxt 交易所实例，并根据 market_type 设置默认类型。
        这里不做太重的交易所适配，只做通用部分。
        """
        if not keys:
            # 没有配置就返回 None，用的时候要检查
            return None

        exchange_class = getattr(ccxt, self.exchange_id)
        exchange = exchange_class({
            "apiKey": keys.get("apiKey"),
            "secret": keys.get("secret"),
            "password": keys.get("password"),   # 有些交易所（OKX）需要
            "enableRateLimit": True,
        })

        # 对支持的交易所启用 sandbox（如果你用的是 test 网站）
        if env == Env.TEST:
            try:
                exchange.set_sandbox_mode(True)
            except Exception:
                # 有些交易所可能不支持 sandbox，忽略
                pass

        # 根据现货 / 合约 设置默认类型
        if market_type == MarketType.SPOT:
            exchange.options["defaultType"] = "spot"
        else:
            # 各家叫法不同，这里用 swap，OKX/binance 都能理解
            exchange.options["defaultType"] = "swap"

        return exchange

    def _choose_client(self, env: Env, market: MarketType):
        """
        根据环境 + 市场类型，选择对应的 client
        """
        if market == MarketType.SPOT:
            return self._spot_test if env == Env.TEST else self._spot_live
        else:
            return self._futures_test if env == Env.TEST else self._futures_live

    # ----- 对外暴露的核心方法：下单 -----

    def place_order(self, req: OrderRequest) -> OrderResult:
        """
        主程序/策略调用唯一入口：
        - 现货买卖
        - 合约开多/开空/平仓（通过 side + position_side + reduce_only 表示）
        """
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

        # 市价 or 限价
        order_type = "market" if req.price is None else "limit"

        params: Dict[str, Any] = {}

        # 合约下单的一些额外参数（不同交易所细节可能不同）
        if req.market == MarketType.FUTURES:
            # positionSide: LONG / SHORT
            if req.position_side is not None:
                # 不同交易所字段名可能不同，这里先用通用的
                params["positionSide"] = req.position_side.name.upper()

            # reduceOnly 平仓
            if req.reduce_only:
                params["reduceOnly"] = True

            # 杠杆设置：有些交易所支持 set_leverage，有些需要在下单时带参数
            if req.leverage is not None:
                try:
                    # 并不是所有交易所都实现了这个方法，失败就忽略
                    client.set_leverage(req.leverage, req.symbol)
                except Exception:
                    # 如果失败，继续往下走，用默认杠杆
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

    # ----- 给企业微信用的“精简文本格式” -----

    @staticmethod
    def format_wecom_message(req: OrderRequest, res: OrderResult) -> str:
        """
        把一次下单请求 + 结果，格式化成一段简洁的文本，适合推送到企业微信
        """
        env_tag = "TEST" if req.env == Env.TEST else "LIVE"
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
