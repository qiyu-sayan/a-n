import os
import sys
import traceback
from datetime import datetime
import time

def okx_bar(interval: str) -> str:
    """
    把通用 interval 转成 OKX 接受的 bar 参数
    """
    m = {
        "1m": "1m",
        "3m": "3m",
        "5m": "5m",
        "15m": "15m",
        "30m": "30m",

        "1h": "1H",
        "2h": "2H",
        "4h": "4H",
        "6h": "6H",
        "12h": "12H",

        "1d": "1D",
        "1w": "1W"
    }
    k = interval.strip().lower()
    return m.get(k, interval)


# 包内导入
from .trader import OKXTrader, load_config
from .strategy import generate_signal

# 根目录的企业微信推送：兼容多种函数名，如果都没有就退化成打印
try:
    from wecom_notify import send_text as send_wecom_text
except ImportError:
    try:
        from wecom_notify import send_markdown as send_wecom_text
    except ImportError:
        def send_wecom_text(msg: str) -> None:
            print(f"[WECOM MOCK] {msg}")


def symbol_to_inst_id(symbol: str) -> str:
    """
    你配置里用 BTCUSDT / ETHUSDT 的写法，这里统一转成 OKX instId：BTC-USDT-SWAP
    """
    s = symbol.upper().replace("-", "").replace("_", "")
    if s.endswith("USDT"):
        base = s[:-4]
        return f"{base}-USDT-SWAP"
    return symbol


def notify_order(action: str, symbol: str, side: str, price: float, size: float | None, extra: str = "") -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    msg = f"[{ts}] {action} {symbol} ({side}) @ {price}"
    if size is not None:
        msg += f" size={size}"
    if extra:
        msg += f"\n{extra}"
    send_wecom_text(msg)


def _format_signal_extra(info: dict) -> str:
    if not isinstance(info, dict):
        return ""
    # 只挑关键字段，避免刷屏
    keys = ["trend", "reason", "rsi", "macd", "ema", "atr", "score"]
    lines = []
    for k in keys:
        if k in info:
            lines.append(f"- {k}: {info.get(k)}")
    return "\n".join(lines)


def run_once(cfg: dict) -> None:
    env = os.getenv("BOT_ENV", "test").lower()
    use_demo = env != "live"
    print(f"[ENV] BOT_ENV={env}, use_demo={use_demo}")

    interval = cfg.get("interval", "1h")
    bar = okx_bar(interval)
    htf_bar = okx_bar(cfg.get("htf_bar", "4h"))

    print(f"Running bot once, interval={interval}, bar={bar}, htf_bar={htf_bar}")

    trader = OKXTrader(cfg, use_demo=use_demo)

    risk_conf = cfg.get("risk", {})
    max_pos_pct = float(risk_conf.get("max_pos", 0.005))

    symbols = cfg.get("symbols", [])
    for symbol in symbols:
        inst_id = symbol_to_inst_id(symbol)

        try:
            last = trader.get_last_price(inst_id)
        except Exception as e:
            print(f"[ERROR] get_last_price failed for {symbol}: {e}")
            continue

        # ---------- 2.1 风控检查：已有持仓先看要不要平 ----------
        # （这部分你原来就有，后面我们会在 trader.py 里把 TP/SL 托管和 5s 风控补齐）
        try:
            positions = trader.get_positions(inst_id)
        except Exception as e:
            print(f"[ERROR][RISK] get_positions failed for {symbol}: {e}")
            positions = []

        # ---------- 2.2 拉 K 线 ----------
        try:
            klines = trader.get_candles(inst_id, bar=bar, limit=int(cfg.get("limit", 200)))
        except Exception as e:
            print(f"[ERROR] get_candles failed for {symbol}: {e}")
            continue

        htf_klines = None
        try:
            if htf_bar:
                htf_klines = trader.get_candles(inst_id, bar=htf_bar, limit=int(cfg.get("htf_limit", 200)))
        except Exception as e:
            print(f"[WARN] get htf candles failed for {symbol}: {e}")

        # ---------- 2.3 生成策略信号 ----------
        try:
            signal, info = generate_signal(
                symbol=symbol,
                klines=klines,
                cfg=cfg,
                htf_klines=htf_klines,
                debug=True,
            )
        except Exception as e:
            print(f"[ERROR][STRATEGY] generate_signal failed for {symbol}: {e}")
            continue

        if not signal:
            print(f"[NO SIGNAL] {symbol}: no clear signal, do nothing.")
            continue

        # ---------- 2.4 根据信号下单 ----------
        if signal == "LONG":
            try:
                resp = trader.open_long(inst_id, ref_price=last, max_pos_pct=max_pos_pct)
                print(f"[DEBUG] open_long resp: {resp}")
                notify_order(
                    action="开多",
                    symbol=symbol,
                    side="多",
                    price=last,
                    size=None,
                    extra=_format_signal_extra(info),
                )
            except Exception as e:
                print(f"[ERROR] open_long failed for {symbol}: {e}")

        elif signal == "SHORT":
            try:
                resp = trader.open_short(inst_id, ref_price=last, max_pos_pct=max_pos_pct)
                print(f"[DEBUG] open_short resp: {resp}")
                notify_order(
                    action="开空",
                    symbol=symbol,
                    side="空",
                    price=last,
                    size=None,
                    extra=_format_signal_extra(info),
                )
            except Exception as e:
                print(f"[ERROR] open_short failed for {symbol}: {e}")
        else:
            print(f"[NO ACTION] {symbol}: signal={signal}")

    print("Run once done.")


def run_daemon(cfg: dict) -> None:
    """
    连续运行模式（双循环骨架）：
    - entry_loop：每 entry_interval_sec 扫描一次信号（默认 900s = 15m）
    - risk_loop：每 risk_loop_interval 秒检查一次仓位变化（默认 5s）

    注意：
    - 真正的“实时止盈止损”我们会在 trader.py 里用 OKX 托管 TP/SL（attachAlgoOrds）实现；
      risk_loop 这里主要负责：手动平仓/外部平仓识别 + 推送兜底。
    """
    env = os.getenv("BOT_ENV", "test").lower()
    use_demo = env != "live"
    print(f"[ENV] BOT_ENV={env}, use_demo={use_demo}")

    entry_interval_sec = int(cfg.get("entry_interval_sec", 15 * 60))
    risk_loop_interval = int(cfg.get("risk_loop_interval", 5))
    print(f"[DAEMON] entry_interval_sec={entry_interval_sec}, risk_loop_interval={risk_loop_interval}")

    trader = OKXTrader(cfg, use_demo=use_demo)

    # 仓位快照：用于检测仓位从有到无（疑似手动/托管平仓）
    last_pos: dict[str, float] = {}
    last_entry_ts = 0.0

    while True:
        now = time.time()

        # 1) risk_loop：仓位变化轮询
        try:
            positions = trader.get_positions()
            cur: dict[str, float] = {}
            for p in positions:
                inst = p.get("instId")
                pos = float(p.get("pos") or 0)
                if inst:
                    cur[inst] = pos

            # 检测：从有仓位到无仓位（后面 trader.py 会细分成 MANUAL/TP/SL）
            for inst, prev_pos in list(last_pos.items()):
                if prev_pos and (cur.get(inst, 0.0) == 0.0):
                    send_wecom_text(
                        f"【检测到平仓】{inst} 仓位从 {prev_pos} → 0（可能：手动平仓/交易所止盈止损触发）"
                    )

            last_pos = cur
        except Exception as e:
            print(f"[WARN] risk_loop error: {e}")

        # 2) entry_loop：低频扫描信号
        if now - last_entry_ts >= entry_interval_sec:
            try:
                print(f"[DAEMON] entry tick @ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
                run_once(cfg)  # 复用现有逻辑（不破坏你原本的运行方式）
            except Exception as e:
                print(f"[ERROR] entry_loop failed: {e}")
            finally:
                last_entry_ts = now

        time.sleep(risk_loop_interval)


def main() -> None:
    cfg = load_config()

    # daemon 开关：
    # 1) params.json 里设置 "daemon": true
    # 2) 或设置环境变量 BOT_DAEMON=1
    daemon = bool(cfg.get("daemon")) or os.getenv("BOT_DAEMON", "0") == "1"

    if daemon:
        run_daemon(cfg)
    else:
        run_once(cfg)


if __name__ == "__main__":
    main()
