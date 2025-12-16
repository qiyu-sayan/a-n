import os
import json
import time
import csv
from datetime import datetime
from typing import Dict, List, Optional

from okx import Trade, Account
try:
    # 一些版本叫 MarketData
    from okx import MarketData as Market
except ImportError:
    # 少数版本才叫 Market
    from okx import Market


from .wecom_notify import send_text as wecom_notify


def load_config(path: str = "params.json") -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


class OKXTrader:
    def __init__(self, cfg: dict, use_demo: bool = True):
        self.cfg = cfg
        self.use_demo = use_demo

        api_key = os.getenv("OKX_API_KEY")
        api_secret = os.getenv("OKX_API_SECRET")
        passphrase = os.getenv("OKX_API_PASSPHRASE")

        flag = "1" if use_demo else "0"

        self.trade = Trade.TradeAPI(api_key, api_secret, passphrase, flag=flag)
        self.account = Account.AccountAPI(api_key, api_secret, passphrase, flag=flag)
        self.market = Market.MarketAPI(flag=flag)

        # === trade journal ===
        self.journal_path = cfg.get("trade_journal_path", "trade_journal.csv")
        self._init_journal()

        # === position snapshot ===
        self.last_positions: Dict[str, float] = {}

    # ------------------------------------------------------------------
    # Journal & Stats
    # ------------------------------------------------------------------
    def _init_journal(self):
        if os.path.exists(self.journal_path):
            return
        with open(self.journal_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow([
                "time",
                "symbol",
                "side",
                "leverage",
                "entry_price",
                "exit_price",
                "size",
                "notional",
                "pnl_usdt",
                "pnl_pct",
                "reason",
            ])

    def record_trade(
        self,
        symbol: str,
        side: str,
        leverage: int,
        entry_price: float,
        exit_price: float,
        size: float,
        notional: float,
        reason: str,
    ):
        pnl = (exit_price - entry_price) * size
        if side == "SHORT":
            pnl = -pnl

        pnl_pct = pnl / notional if notional else 0.0

        with open(self.journal_path, "a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow([
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                symbol,
                side,
                leverage,
                entry_price,
                exit_price,
                size,
                round(notional, 4),
                round(pnl, 4),
                round(pnl_pct * 100, 2),
                reason,
            ])

    # ------------------------------------------------------------------
    # Market & Position
    # ------------------------------------------------------------------
    def get_last_price(self, inst_id: str) -> float:
        r = self.market.get_ticker(instId=inst_id)
        return float(r["data"][0]["last"])

    def get_positions(self, inst_id: Optional[str] = None) -> List[dict]:
        r = self.account.get_positions(instId=inst_id)
        return r.get("data", [])

    def sync_positions(self) -> Dict[str, float]:
        """
        返回 {instId: pos}
        """
        pos_map: Dict[str, float] = {}
        for p in self.get_positions():
            inst = p.get("instId")
            pos = float(p.get("pos") or 0)
            if inst:
                pos_map[inst] = pos
        return pos_map

    # ------------------------------------------------------------------
    # Order with TP / SL
    # ------------------------------------------------------------------
    def place_order_with_tp_sl(
        self,
        inst_id: str,
        side: str,
        size: float,
        leverage: int,
        entry_price: float,
        tp_pct: float,
        sl_pct: float,
    ):
        """
        在 OKX 下单并直接挂托管 TP / SL
        """
        pos_side = "long" if side == "LONG" else "short"
        ord_side = "buy" if side == "LONG" else "sell"

        # 计算 TP / SL 价格
        if side == "LONG":
            tp_price = round(entry_price * (1 + tp_pct), 4)
            sl_price = round(entry_price * (1 - sl_pct), 4)
        else:
            tp_price = round(entry_price * (1 - tp_pct), 4)
            sl_price = round(entry_price * (1 + sl_pct), 4)

        attach = [{
            "tpTriggerPx": str(tp_price),
            "tpOrdPx": "-1",
            "slTriggerPx": str(sl_price),
            "slOrdPx": "-1",
        }]

        r = self.trade.place_order(
            instId=inst_id,
            tdMode="cross",
            side=ord_side,
            ordType="market",
            sz=str(size),
            posSide=pos_side,
            lever=str(leverage),
            attachAlgoOrds=attach,
        )

        if r.get("code") != "0":
            wecom_notify(f"【严重】TP/SL 挂单失败：{inst_id}\n{r}")
            raise RuntimeError(r)

        return r

    # ------------------------------------------------------------------
    # Open Long / Short
    # ------------------------------------------------------------------
    def _calc_size_and_leverage(self, inst_id: str, ref_price: float):
        symbol = inst_id.split("-")[0]
        risk = self.cfg.get("risk", {})
        lev_cfg = risk.get("leverage", {})
        leverage = lev_cfg.get(symbol, lev_cfg.get("DEFAULT", 3))

        max_notional = risk.get("max_notional_usdt_per_symbol", 600)
        max_contracts = risk.get("max_contracts_per_symbol", 3)

        # 以 USDT 计的名义价值
        notional = min(max_notional, ref_price * max_contracts)

        size = notional / ref_price
        size = min(size, max_contracts)

        return round(size, 4), leverage, notional

    def open_long(self, inst_id: str, ref_price: float, max_pos_pct: float):
        return self._open(inst_id, "LONG", ref_price)

    def open_short(self, inst_id: str, ref_price: float, max_pos_pct: float):
        return self._open(inst_id, "SHORT", ref_price)

    def _open(self, inst_id: str, side: str, ref_price: float):
        symbol = inst_id.split("-")[0]
        size, leverage, notional = self._calc_size_and_leverage(inst_id, ref_price)

        tp_sl = self.cfg.get("tp_sl", {})
        cfg_tp_sl = tp_sl.get(symbol, tp_sl.get("DEFAULT"))
        tp_pct = cfg_tp_sl["tp"]
        sl_pct = cfg_tp_sl["sl"]

        r = self.place_order_with_tp_sl(
            inst_id=inst_id,
            side=side,
            size=size,
            leverage=leverage,
            entry_price=ref_price,
            tp_pct=tp_pct,
            sl_pct=sl_pct,
        )

        return r
