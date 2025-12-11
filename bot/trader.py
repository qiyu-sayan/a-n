import os
import time
import json
import hmac
import base64
import hashlib
from typing import Any, Dict, Optional, List

import requests


CONFIG_PATH = os.environ.get("BOT_CONFIG_PATH", "config/params.json")


def load_config(path: str = CONFIG_PATH) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


class OKXAPIError(Exception):
    pass


class OKXTrader:
    """
    OKX 合约交易封装：
    - 支持开多 / 平多 / 开空 / 平空（双向持仓）
    - 自动按 USDT 风险换算为“张”
    - 支持市价 / 限价
    - 支持附带止盈止损（attachAlgoOrds）
    """

    def __init__(
        self,
        config: Dict[str, Any],
        api_key: Optional[str] = None,
        api_secret: Optional[str] = None,
        passphrase: Optional[str] = None,
        use_demo: bool = True,
        base_url: str = "https://www.okx.com",
        td_mode: str = "cross",
        lever: int = 5,
    ) -> None:
        self.config = config
        self.risk_cfg = config.get("risk", {})
        self.exec_cfg = config.get("execution", {})

        self.api_key = api_key or os.environ.get("OKX_API_KEY", "")
        self.api_secret = api_secret or os.environ.get("OKX_API_SECRET", "")
        self.passphrase = passphrase or os.environ.get("OKX_API_PASSPHRASE", "")

        self.base_url = base_url.rstrip("/")
        self.use_demo = use_demo  # 模拟盘：header x-simulated-trading=1
        self.td_mode = td_mode    # "cross" 或 "isolated"
        self.lever = lever        # 默认杠杆倍数

        if not (self.api_key and self.api_secret and self.passphrase):
            raise ValueError("OKX API key/secret/passphrase 未配置，请检查环境变量或构造参数。")

    # ---------- 基础 HTTP 封装 ----------

    @staticmethod
    def _timestamp() -> str:
        # OKX 要求 ISO8601，精度到毫秒
        return time.strftime('%Y-%m-%dT%H:%M:%S.000Z', time.gmtime())

    def _sign(self, ts: str, method: str, path: str, body: str) -> str:
        msg = f"{ts}{method.upper()}{path}{body}"
        mac = hmac.new(
            self.api_secret.encode("utf-8"),
            msg.encode("utf-8"),
            hashlib.sha256
        ).digest()
        return base64.b64encode(mac).decode()

    def _request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        body: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        url = self.base_url + path
        ts = self._timestamp()
        body_str = json.dumps(body) if body else ""
        sign = self._sign(ts, method, path, body_str)

        headers = {
            "OK-ACCESS-KEY": self.api_key,
            "OK-ACCESS-SIGN": sign,
            "OK-ACCESS-TIMESTAMP": ts,
            "OK-ACCESS-PASSPHRASE": self.passphrase,
            "Content-Type": "application/json",
        }
        if self.use_demo:
            headers["x-simulated-trading"] = "1"

        resp = requests.request(
            method=method.upper(),
            url=url,
            headers=headers,
            params=params,
            data=body_str if body else None,
            timeout=10,
        )
        data = resp.json()
        if data.get("code") != "0":
            # 打印完整返回，方便在 GitHub Actions 日志里看到 sCode/sMsg
            print("OKX raw error response:", data)
            raise OKXAPIError(f"OKX API error: {data.get('code')} {data.get('msg')}")
        return data


    # ---------- 公共数据 / 账户信息 ----------

    def get_contract_value(self, inst_id: str) -> float:
        """
        获取合约面值（ctVal），用于从 USDT 风险换算“张数”
        """
        path = "/api/v5/public/instruments"
        params = {"instType": "SWAP", "instId": inst_id}
        data = self._request("GET", path, params=params)
        insts = data.get("data", [])
        if not insts:
            # 找不到时，保守假设 1 USDT/张
            return 1.0
        inst = insts[0]
        ct_val = inst.get("ctVal")
        try:
            return float(ct_val)
        except (TypeError, ValueError):
            return 1.0

    def get_last_price(self, inst_id: str) -> float:
        path = "/api/v5/market/ticker"
        params = {"instId": inst_id}
        data = self._request("GET", path, params=params)
        items = data.get("data", [])
        if not items:
            raise OKXAPIError(f"无法获取 {inst_id} 最新价格")
        last = items[0].get("last")
        return float(last)

    def get_equity_usdt(self) -> float:
        """
        获取总权益（USDT），用于根据风险比例算下单价值。
        这里使用 account/balance 的 U 本位合约部分。
        """
        path = "/api/v5/account/balance"
        data = self._request("GET", path)
        total_equity = 0.0
        for acc in data.get("data", []):
            for d in acc.get("details", []):
                # U 本位统一账户，一般币种为 USDT
                if d.get("ccy") == "USDT":
                    eq = d.get("eq")
                    if eq is not None:
                        total_equity += float(eq)
        if total_equity <= 0:
            # 防止为 0 导致无法交易
            total_equity = 100.0
        return total_equity

    def get_positions(self, inst_id: Optional[str] = None) -> List[Dict[str, Any]]:
        path = "/api/v5/account/positions"
        params: Dict[str, Any] = {"instType": "SWAP"}
        if inst_id:
            params["instId"] = inst_id
        data = self._request("GET", path, params=params)
        return data.get("data", [])

    # ---------- 下单相关工具 ----------

    def _calc_order_size_by_risk(
        self,
        inst_id: str,
        ref_price: Optional[float] = None,
        risk_pct: Optional[float] = None,
    ) -> str:
        """
        根据账户权益 & 风险比例，计算下单“张数”
        risk_pct: 使用 risk.max_pos，如果传入则覆盖
        """
        max_pos_pct = risk_pct if risk_pct is not None else float(self.risk_cfg.get("max_pos", 0.05))
        equity = self.get_equity_usdt()
        notional = equity * max_pos_pct  # 计划使用的 USDT 名义价值

        ct_val = self.get_contract_value(inst_id)

        if ct_val <= 0:
            ct_val = 1.0

        # ✅ 正确公式：每张合约的名义价值 ≈ ctVal * 当前价格
        # 计划名义价值 / 每张名义价值 = 张数
        if ref_price and ref_price > 0:
            contract_notional = ct_val * ref_price
            if contract_notional <= 0:
                contract_notional = ref_price  # 兜底
            sz = int(notional / contract_notional)
        else:
            # 没有价格时的兜底：尽量小
            sz = int(notional / ct_val)

        if sz < 1:
            sz = 1

        print(f"[DEBUG] equity={equity}, max_pos_pct={max_pos_pct}, notional={notional}, "
              f"ct_val={ct_val}, ref_price={ref_price}, sz={sz}")

        return str(sz)


    def _build_tp_sl(
        self,
        inst_id: str,
        entry_price: float,
        pos_side: str,
    ) -> Optional[List[Dict[str, Any]]]:
        """
        根据 risk.stop / risk.take 和 execution.tp_sl_price_type 构造止盈止损结构。
        """
        stop_pct = float(self.risk_cfg.get("stop", 0.02))
        take_pct = float(self.risk_cfg.get("take", 0.04))
        px_type = self.exec_cfg.get("tp_sl_price_type", "last")  # last / index / mark

        if stop_pct <= 0 and take_pct <= 0:
            return None

        # 多单：止盈在上方，止损在下方
        # 空单：止盈在下方，止损在上方
        if pos_side == "long":
            tp_px = entry_price * (1.0 + take_pct) if take_pct > 0 else None
            sl_px = entry_price * (1.0 - stop_pct) if stop_pct > 0 else None
        else:
            tp_px = entry_price * (1.0 - take_pct) if take_pct > 0 else None
            sl_px = entry_price * (1.0 + stop_pct) if stop_pct > 0 else None

        algo: Dict[str, Any] = {}
        if tp_px:
            algo["tpTriggerPx"] = f"{tp_px:.4f}"
            algo["tpOrdPx"] = "-1"  # -1 表示以市价平仓
            algo["tpTriggerPxType"] = px_type
        if sl_px:
            algo["slTriggerPx"] = f"{sl_px:.4f}"
            algo["slOrdPx"] = "-1"
            algo["slTriggerPxType"] = px_type

        if not algo:
            return None
        return [algo]

    def _build_price_and_type(
        self,
        inst_id: str,
        side: str,
        ref_price: Optional[float] = None,
    ) -> (str, Optional[str]):
        """
        返回 ordType, px
        - 如果 execution.use_limit = False → 市价单
        - 如果为 True → 限价单，按 limit_offset 做微调
        """
        use_limit = bool(self.exec_cfg.get("use_limit", False))
        limit_offset = float(self.exec_cfg.get("limit_offset", 0.001))

        if not use_limit or ref_price is None:
            return "market", None

        # 买单：价格往上浮，避免挂太低
        # 卖单：价格往下压，避免挂太高
        if side == "buy":
            px = ref_price * (1.0 + limit_offset)
        else:
            px = ref_price * (1.0 - limit_offset)

        return "limit", f"{px:.4f}"

    # ---------- 下单 & 平仓 ----------

    def place_order(
        self,
        inst_id: str,
        side: str,
        pos_side: str,
        sz: str,
        ord_type: str = "market",
        px: Optional[str] = None,
        attach_algo: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """
        核心下单函数：
        - side: "buy" / "sell"
        - pos_side: "long" / "short"
        - sz: "张数"（字符串）
        """
        path = "/api/v5/trade/order"
        body: Dict[str, Any] = {
            "instId": inst_id,
            "tdMode": self.td_mode,   # "cross" / "isolated"
            "side": side,
            "posSide": pos_side,      # "long" / "short"（双向持仓）
            "ordType": ord_type,
            "sz": sz,
            "ccy": "USDT",            # U 本位合约用 USDT 作为保证金
        }


        if px is not None and ord_type == "limit":
            body["px"] = px

        if attach_algo:
            body["attachAlgoOrders"] = attach_algo

        data = self._request("POST", path, body=body)
        return data

    # --------- 对外友好方法：按信号开平仓 ---------

    def open_long(self, inst_id: str, ref_price: Optional[float] = None) -> Dict[str, Any]:
        """
        开多：buy + long
        """
        if ref_price is None:
            ref_price = self.get_last_price(inst_id)

        sz = self._calc_order_size_by_risk(inst_id, ref_price)
        ord_type, px = self._build_price_and_type(inst_id, "buy", ref_price)
        attach_algo = self._build_tp_sl(inst_id, ref_price, pos_side="long")

        return self.place_order(
            inst_id=inst_id,
            side="buy",
            pos_side="long",
            sz=sz,
            ord_type=ord_type,
            px=px,
            attach_algo=attach_algo,
        )

    def open_short(self, inst_id: str, ref_price: Optional[float] = None) -> Dict[str, Any]:
        """
        开空：sell + short
        """
        if ref_price is None:
            ref_price = self.get_last_price(inst_id)

        sz = self._calc_order_size_by_risk(inst_id, ref_price)
        ord_type, px = self._build_price_and_type(inst_id, "sell", ref_price)
        attach_algo = self._build_tp_sl(inst_id, ref_price, pos_side="short")

        return self.place_order(
            inst_id=inst_id,
            side="sell",
            pos_side="short",
            sz=sz,
            ord_type=ord_type,
            px=px,
            attach_algo=attach_algo,
        )

    def close_long(self, inst_id: str, sz: Optional[str] = None) -> Dict[str, Any]:
        """
        平多：sell + long
        - 如果不传 sz，则尝试查当前 long 持仓张数，全平。
        """
        if sz is None:
            sz = self._get_current_pos_size(inst_id, "long")
        if sz is None or sz == "0":
            raise ValueError(f"{inst_id} 当前无多单可平。")

        return self.place_order(
            inst_id=inst_id,
            side="sell",
            pos_side="long",
            sz=sz,
            ord_type="market",
        )

    def close_short(self, inst_id: str, sz: Optional[str] = None) -> Dict[str, Any]:
        """
        平空：buy + short
        - 如果不传 sz，则尝试查当前 short 持仓张数，全平。
        """
        if sz is None:
            sz = self._get_current_pos_size(inst_id, "short")
        if sz is None or sz == "0":
            raise ValueError(f"{inst_id} 当前无空单可平。")

        return self.place_order(
            inst_id=inst_id,
            side="buy",
            pos_side="short",
            sz=sz,
            ord_type="market",
        )

    def _get_current_pos_size(self, inst_id: str, pos_side: str) -> Optional[str]:
        """
        查某个 inst_id + posSide 的当前持仓张数。
        """
        positions = self.get_positions(inst_id)
        for p in positions:
            if p.get("instId") == inst_id and p.get("posSide") == pos_side:
                sz = p.get("pos")
                if sz is not None:
                    return sz
        return None

    def close_all(self, inst_id: str) -> None:
        """
        简单全平：如果有多单就平多，有空单就平空。
        """
        long_sz = self._get_current_pos_size(inst_id, "long")
        if long_sz and long_sz != "0":
            self.close_long(inst_id, long_sz)

        short_sz = self._get_current_pos_size(inst_id, "short")
        if short_sz and short_sz != "0":
            self.close_short(inst_id, short_sz)


if __name__ == "__main__":
    """
    简单自测用：不会真的下单，除非你填好密钥并去掉注释。
    建议你在项目里从别的文件调用，不要直接跑这个。
    """
    cfg = load_config()
    # 示例：从环境变量读取密钥，模拟盘
    # trader = OKXTrader(cfg, use_demo=True)
    # print(trader.get_last_price("BTC-USDT-SWAP"))
    pass
