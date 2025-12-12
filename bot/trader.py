import os
import time
import json
import hmac
import base64
import hashlib
from typing import Any, Dict, Optional, List

import requests
import urllib.parse
import pandas as pd
from datetime import datetime, timezone


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
        self.use_demo = use_demo
        self.td_mode = td_mode
        self.lever = lever

        if not (self.api_key and self.api_secret and self.passphrase):
            raise ValueError("OKX API key/secret/passphrase 未配置，请检查环境变量或构造参数。")

    @staticmethod
    def _timestamp() -> str:
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

        if params:
            query_str = urllib.parse.urlencode(params)
            sign_path = f"{path}?{query_str}"
        else:
            sign_path = path

        sign = self._sign(ts, method, sign_path, body_str)

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
            print("OKX raw error response:", data)
            raise OKXAPIError(f"OKX API error: {data.get('code')} {data.get('msg')}")
        return data

    def get_contract_value(self, inst_id: str) -> float:
        path = "/api/v5/public/instruments"
        params = {"instType": "SWAP", "instId": inst_id}
        data = self._request("GET", path, params=params)
        insts = data.get("data", [])
        if not insts:
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
        path = "/api/v5/account/balance"
        data = self._request("GET", path)
        total_equity = 0.0
        for acc in data.get("data", []):
            for d in acc.get("details", []):
                if d.get("ccy") == "USDT":
                    eq = d.get("eq")
                    if eq is not None:
                        total_equity += float(eq)
        if total_equity <= 0:
            total_equity = 100.0
        return total_equity

    def get_positions(self, inst_id: Optional[str] = None) -> List[Dict[str, Any]]:
        path = "/api/v5/account/positions"
        params: Dict[str, Any] = {"instType": "SWAP"}
        if inst_id:
            params["instId"] = inst_id
        data = self._request("GET", path, params=params)
        return data.get("data", [])

    def _calc_order_size_by_risk(
        self,
        inst_id: str,
        ref_price: Optional[float] = None,
        risk_pct: Optional[float] = None,
    ) -> str:
        max_pos_pct = risk_pct if risk_pct is not None else float(self.risk_cfg.get("max_pos", 0.05))
        equity = self.get_equity_usdt()
        notional = equity * max_pos_pct

        ct_val = self.get_contract_value(inst_id)
        if ct_val <= 0:
            ct_val = 1.0

        if ref_price and ref_price > 0:
            contract_notional = ct_val * ref_price
            if contract_notional <= 0:
                contract_notional = ref_price
            sz = int(notional / contract_notional)
        else:
            sz = int(notional / ct_val)

        if sz < 1:
            sz = 1

        print(f"[DEBUG] equity={equity}, max_pos_pct={max_pos_pct}, notional={notional}, "
              f"ct_val={ct_val}, ref_price={ref_price}, sz={sz}")

        return str(sz)

    def _build_tp_sl(self, inst_id: str, entry_price: float, pos_side: str) -> Optional[List[Dict[str, Any]]]:
        stop_pct = float(self.risk_cfg.get("stop", 0.02))
        take_pct = float(self.risk_cfg.get("take", 0.04))
        px_type = self.exec_cfg.get("tp_sl_price_type", "last")

        if stop_pct <= 0 and take_pct <= 0:
            return None

        if pos_side == "long":
            tp_px = entry_price * (1.0 + take_pct) if take_pct > 0 else None
            sl_px = entry_price * (1.0 - stop_pct) if stop_pct > 0 else None
        else:
            tp_px = entry_price * (1.0 - take_pct) if take_pct > 0 else None
            sl_px = entry_price * (1.0 + stop_pct) if stop_pct > 0 else None

        algo: Dict[str, Any] = {}
        if tp_px:
            algo["tpTriggerPx"] = f"{tp_px:.4f}"
            algo["tpOrdPx"] = "-1"
            algo["tpTriggerPxType"] = px_type
        if sl_px:
            algo["slTriggerPx"] = f"{sl_px:.4f}"
            algo["slOrdPx"] = "-1"
            algo["slTriggerPxType"] = px_type

        if not algo:
            return None
        return [algo]

    def _build_price_and_type(self, inst_id: str, side: str, ref_price: Optional[float] = None) -> (str, Optional[str]):
        use_limit = bool(self.exec_cfg.get("use_limit", False))
        limit_offset = float(self.exec_cfg.get("limit_offset", 0.001))

        if not use_limit or ref_price is None:
            return "market", None

        if side == "buy":
            px = ref_price * (1.0 + limit_offset)
        else:
            px = ref_price * (1.0 - limit_offset)

        return "limit", f"{px:.4f}"

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
        path = "/api/v5/trade/order"
        body: Dict[str, Any] = {
            "instId": inst_id,
            "tdMode": self.td_mode,
            "side": side,
            "posSide": pos_side,
            "ordType": ord_type,
            "sz": sz,
            "ccy": "USDT",
        }

        if px is not None and ord_type == "limit":
            body["px"] = px

        if attach_algo:
            body["attachAlgoOrders"] = attach_algo

        data = self._request("POST", path, body=body)
        return data

    # --------- 对外友好方法：按信号开平仓（兼容 main.py 的 max_pos_pct 调用） ---------

    def open_long(self, inst_id: str, ref_price: Optional[float] = None, max_pos_pct: Optional[float] = None) -> Dict[str, Any]:
        if ref_price is None:
            ref_price = self.get_last_price(inst_id)

        sz = self._calc_order_size_by_risk(inst_id, ref_price, risk_pct=max_pos_pct)
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

    def open_short(self, inst_id: str, ref_price: Optional[float] = None, max_pos_pct: Optional[float] = None) -> Dict[str, Any]:
        if ref_price is None:
            ref_price = self.get_last_price(inst_id)

        sz = self._calc_order_size_by_risk(inst_id, ref_price, risk_pct=max_pos_pct)
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
        positions = self.get_positions(inst_id)
        for p in positions:
            if p.get("instId") == inst_id and p.get("posSide") == pos_side:
                sz = p.get("pos")
                if sz is not None:
                    return sz
        return None

    def close_all(self, inst_id: str) -> None:
        long_sz = self._get_current_pos_size(inst_id, "long")
        if long_sz and long_sz != "0":
            self.close_long(inst_id, long_sz)

        short_sz = self._get_current_pos_size(inst_id, "short")
        if short_sz and short_sz != "0":
            self.close_short(inst_id, short_sz)

    def get_klines(self, inst_id: str, bar: str = "1H", limit: int = 200):
        path = "/api/v5/market/candles"
        params = {"instId": inst_id, "bar": bar, "limit": str(limit)}
        resp = self._request("GET", path, params=params)

        if isinstance(resp, dict):
            code = resp.get("code")
            if code is not None and code != "0":
                print(f"OKX raw error response in get_klines: {resp}")
                return []
            data = resp.get("data", [])
        else:
            data = resp

        rows = []
        for item in reversed(data):
            if isinstance(item, dict):
                ts = item.get("ts") or item.get("t") or item.get("time")
                o = item.get("o") or item.get("open")
                h = item.get("h") or item.get("high")
                l = item.get("l") or item.get("low")
                c = item.get("c") or item.get("close") or o
                vol = item.get("vol") or item.get("volume") or 0
            else:
                if len(item) < 5:
                    continue
                ts = item[0]
                o = item[1]
                h = item[2]
                l = item[3]
                c = item[4]
                vol = item[5] if len(item) > 5 else 0

            try:
                rows.append([int(ts), float(o), float(h), float(l), float(c), float(vol)])
            except Exception:
                continue

        return rows


if __name__ == "__main__":
    cfg = load_config()
    pass
