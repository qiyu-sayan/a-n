import os
import json
import csv
from datetime import datetime
from typing import Dict, List, Optional, Tuple

# ✅ 最稳的 OKX SDK 导入方式（避免 __init__.py 导出差异）
from okx.Trade import TradeAPI
from okx.Account import AccountAPI
from okx.MarketData import MarketAPI


def _wecom_send_text(msg: str) -> None:
    """
    兼容你项目里不同版本的 wecom_notify.py：
    - 优先使用 send_text / send_markdown / notify_error
    - 没有就退化为 print
    """
    try:
        from wecom_notify import send_text
        send_text(msg)
        return
    except Exception:
        pass

    try:
        from wecom_notify import send_markdown
        send_markdown(msg)
        return
    except Exception:
        pass

    print("[WECOM MOCK]", msg)


def _wecom_notify_error(title: str, detail: str) -> None:
    try:
        from wecom_notify import notify_error
        notify_error(title, detail)
        return
    except Exception:
        _wecom_send_text(f"【异常】{title}\n{detail}")


def load_config(path: str = "params.json") -> dict:
    """
    兼容 GitHub Actions / 本地 / 子目录运行：
    1) 优先读取环境变量 BOT_CONFIG（若设置）
    2) 依次尝试：传入 path、仓库根目录 params.json、config/params.json、bot/params.json
    """
    # 1) 环境变量强制指定
    env_path = os.getenv("BOT_CONFIG", "").strip()
    candidates = []
    if env_path:
        candidates.append(Path(env_path))

    # 2) 传入路径（相对/绝对都行）
    candidates.append(Path(path))

    # 3) 以当前文件定位（bot/trader.py -> 仓库根目录）
    here = Path(__file__).resolve()
    repo_root = here.parents[1]  # bot/.. -> repo root
    candidates += [
        repo_root / "params.json",
        repo_root / "config" / "params.json",
        repo_root / "bot" / "params.json",
    ]

    for p in candidates:
        try:
            p = p.expanduser().resolve()
            if p.exists():
                with open(p, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
                cfg["_config_path"] = str(p)
                return cfg
        except Exception:
            continue

    raise FileNotFoundError(
        f"找不到配置文件。已尝试: {[str(c) for c in candidates]}"
    )


class OKXTrader:
    """
    本文件职责（本轮重点）：
    - 统一 OKX SDK 导入方式（兼容 python-okx 版本）
    - 下单时挂 OKX 托管 TP/SL（attachAlgoOrds）
    - 交易日志 trade_journal.csv 记录（每单）
    - 供 main.py 调用：get_last_price / get_positions / open_long / open_short / get_candles
    """

    def __init__(self, cfg: dict, use_demo: bool = True):
        self.cfg = cfg
        self.use_demo = use_demo

        api_key = os.getenv("OKX_API_KEY")
        api_secret = os.getenv("OKX_API_SECRET")
        passphrase = os.getenv("OKX_API_PASSPHRASE")

        if not api_key or not api_secret or not passphrase:
            _wecom_notify_error(
                "OKX 密钥缺失",
                "请检查 GitHub Secrets：OKX_API_KEY / OKX_API_SECRET / OKX_API_PASSPHRASE",
            )
            raise RuntimeError("Missing OKX API credentials.")

        # flag: "1"=模拟盘, "0"=实盘（python-okx 的习惯）
        flag = "1" if use_demo else "0"

        # ✅ 使用模块路径 API（最稳）
        self.trade = TradeAPI(api_key, api_secret, passphrase, flag=flag)
        self.account = AccountAPI(api_key, api_secret, passphrase, flag=flag)
        self.market = MarketAPI(flag=flag)

        # trade journal
        self.journal_path = cfg.get("trade_journal_path", "trade_journal.csv")
        self._init_journal()

        # position snapshot（给 main 的 risk loop 用）
        self.last_positions: Dict[str, float] = {}

    # ------------------------------------------------------------------
    # Journal
    # ------------------------------------------------------------------
    def _init_journal(self) -> None:
        if os.path.exists(self.journal_path):
            return
        with open(self.journal_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(
                [
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
                ]
            )

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
    ) -> Tuple[float, float]:
        """
        写入 trade_journal.csv，并返回 (pnl_usdt, pnl_pct)
        """
        pnl = (exit_price - entry_price) * size
        if side == "SHORT":
            pnl = -pnl

        pnl_pct = (pnl / notional) if notional else 0.0

        with open(self.journal_path, "a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(
                [
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    symbol,
                    side,
                    leverage,
                    entry_price,
                    exit_price,
                    size,
                    round(notional, 6),
                    round(pnl, 6),
                    round(pnl_pct * 100, 4),
                    reason,
                ]
            )
        return float(pnl), float(pnl_pct * 100)

    # ------------------------------------------------------------------
    # Market / Candles / Positions
    # ------------------------------------------------------------------
    def get_last_price(self, inst_id: str) -> float:
        r = self.market.get_ticker(instId=inst_id)
        return float(r["data"][0]["last"])

    def get_candles(self, inst_id: str, bar: str = "15m", limit: int = 200) -> list:
        """
        OKX 行情K线（MarketData）
        """
        r = self.market.get_candlesticks(instId=inst_id, bar=bar, limit=str(limit))
        return r.get("data", [])

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
    # Order sizing & leverage
    # ------------------------------------------------------------------
    def _calc_size_and_leverage(self, inst_id: str, ref_price: float) -> Tuple[float, int, float]:
        """
        下单尺寸：加入两道闸门，避免最小张数导致名义金额被顶大
        - max_notional_usdt_per_symbol
        - max_contracts_per_symbol
        """
        symbol = inst_id.split("-")[0]
        risk = self.cfg.get("risk", {})

        lev_cfg = risk.get("leverage", {})
        leverage = int(lev_cfg.get(symbol, lev_cfg.get("DEFAULT", 3)))

        max_notional = float(risk.get("max_notional_usdt_per_symbol", 600))
        max_contracts = float(risk.get("max_contracts_per_symbol", 3))

        # 这里的 size 先按“名义价值/价格”近似（后续如果你要严格按合约张面值，再补）
        notional = min(max_notional, ref_price * max_contracts)
        size = notional / ref_price

        # 张数上限闸门
        size = min(size, max_contracts)

        # 最后截断
        size = round(size, 4)
        notional = float(size * ref_price)
        return size, leverage, notional

    # ------------------------------------------------------------------
    # Place order with TP/SL (托管)
    # ------------------------------------------------------------------
    def place_order_with_tp_sl(
        self,
        inst_id: str,
        side: str,  # "LONG" | "SHORT"
        size: float,
        leverage: int,
        entry_price: float,
        tp_pct: float,
        sl_pct: float,
    ):
        """
        市价开仓 + OKX 托管 TP/SL（attachAlgoOrds）
        """
        pos_side = "long" if side == "LONG" else "short"
        ord_side = "buy" if side == "LONG" else "sell"

        # 计算 TP/SL 价格
        if side == "LONG":
            tp_price = entry_price * (1 + tp_pct)
            sl_price = entry_price * (1 - sl_pct)
        else:
            tp_price = entry_price * (1 - tp_pct)
            sl_price = entry_price * (1 + sl_pct)

        # OKX 常用：tpOrdPx / slOrdPx = -1 表示市价委托
        attach = [
            {
                "tpTriggerPx": str(round(tp_price, 4)),
                "tpOrdPx": "-1",
                "slTriggerPx": str(round(sl_price, 4)),
                "slOrdPx": "-1",
            }
        ]

        resp = self.trade.place_order(
            instId=inst_id,
            tdMode="cross",
            side=ord_side,
            ordType="market",
            sz=str(size),
            posSide=pos_side,
            lever=str(leverage),
            attachAlgoOrds=attach,
        )

        if resp.get("code") != "0":
            _wecom_notify_error("TP/SL 托管下单失败", json.dumps(resp, ensure_ascii=False))
            raise RuntimeError(resp)

        return resp

    # ------------------------------------------------------------------
    # Public open methods (for main.py)
    # ------------------------------------------------------------------
    def open_long(self, inst_id: str, ref_price: float, max_pos_pct: float = 0.0):
        return self._open(inst_id, "LONG", ref_price)

    def open_short(self, inst_id: str, ref_price: float, max_pos_pct: float = 0.0):
        return self._open(inst_id, "SHORT", ref_price)

    def _open(self, inst_id: str, side: str, ref_price: float):
        symbol = inst_id.split("-")[0]

        size, leverage, notional = self._calc_size_and_leverage(inst_id, ref_price)

        # tp/sl 配置：symbol 优先，其次 DEFAULT
        tp_sl = self.cfg.get("tp_sl", {})
        cfg_tp_sl = tp_sl.get(symbol, tp_sl.get("DEFAULT"))
        if not cfg_tp_sl:
            raise RuntimeError(f"Missing tp_sl config for {symbol} and DEFAULT")

        tp_pct = float(cfg_tp_sl["tp"])
        sl_pct = float(cfg_tp_sl["sl"])

        resp = self.place_order_with_tp_sl(
            inst_id=inst_id,
            side=side,
            size=size,
            leverage=leverage,
            entry_price=ref_price,
            tp_pct=tp_pct,
            sl_pct=sl_pct,
        )

        # 下单成功也可以轻量提示（避免刷屏你可以后面关）
        _wecom_send_text(
            f"✅ 下单成功（托管TP/SL）\n{inst_id} {side}\nprice={ref_price} size={size} lev={leverage}x\nTP={tp_pct*100:.2f}% SL={sl_pct*100:.2f}%"
        )

        return resp
