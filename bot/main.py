import os
from typing import Dict, Any, List, Optional, Tuple

from bot.trader import OKXTrader, load_config
from bot.strategy import generate_signal

try:
    # 项目根目录下的企业微信通知脚本
    from wecom_notify import send_text as send_wecom_text
except ImportError:
    # 如果没配置企业微信，也不影响核心逻辑
    def send_wecom_text(msg: str) -> None:
        print(f"[WECOM MOCK] {msg}")


# ----------------------- 工具函数 -----------------------


def symbol_to_inst_id(symbol: str) -> str:
    """
    目前你的 OKX 合约都是 USDT 本位永续，形如：
    BTCUSDT -> BTC-USDT-SWAP
    """
    base = symbol.replace("USDT", "")
    return f"{base}-USDT-SWAP"


def pick_side_position(
    positions: List[Dict[str, Any]], side: str
) -> Optional[Dict[str, Any]]:
    """
    从 OKX 持仓列表中挑出指定 posSide 的持仓。
    side: "long" / "short"
    """
    for p in positions:
        try:
            if p.get("posSide") == side and float(p.get("pos", "0")) != 0:
                return p
        except (TypeError, ValueError):
            continue
    return None


# ------------------- 风险管理：止盈止损 -------------------


def check_risk_close(
    symbol: str,
    inst_id: str,
    last_price: float,
    positions: List[Dict[str, Any]],
    cfg: Dict[str, Any],
    trader: OKXTrader,
    send_wecom_text_func=send_wecom_text,
) -> bool:
    """
    根据配置里的 stop / take，对当前持仓做止盈止损检查。

    返回值:
        True  -> 发生了平仓，本轮应该跳过开新仓
        False -> 没有触发止盈止损
    """
    risk = cfg.get("risk", {})
    stop_pct = float(risk.get("stop", 0.02))   # 默认止损 2%
    take_pct = float(risk.get("take", 0.04))   # 默认止盈 4%

    if not positions:
        return False

    closed_any = False

    for pos in positions:
        try:
            side = pos.get("posSide")          # "long" / "short"
            pos_sz = float(pos.get("pos", "0"))
            if pos_sz == 0:
                continue
            avg_px = float(pos.get("avgPx"))
        except (TypeError, ValueError):
            continue

        if last_price <= 0 or avg_px <= 0:
            continue

        # 计算浮动收益百分比
        if side == "long":
            pnl_pct = (last_price - avg_px) / avg_px
        elif side == "short":
            pnl_pct = (avg_px - last_price) / avg_px
        else:
            continue

        reason = None
        # 止损优先
        if pnl_pct <= -stop_pct:
            reason = (
                f"止损触发：浮动收益 {pnl_pct * 100:.2f}% ≤ "
                f"-{stop_pct * 100:.2f}%"
            )
        # 再看止盈
        elif pnl_pct >= take_pct:
            reason = (
                f"止盈触发：浮动收益 {pnl_pct * 100:.2f}% ≥ "
                f"{take_pct * 100:.2f}%"
            )

        if not reason:
            continue

        if side == "long":
            print(f"[RISK] Closing LONG {symbol} by rule: {reason}")
            # 按仓位数量平多
            trader.close_long(inst_id, abs(pos_sz))
            side_cn = "多"
        else:
            print(f"[RISK] Closing SHORT {symbol} by rule: {reason}")
            trader.close_short(inst_id, abs(pos_sz))
            side_cn = "空"

        # 企业微信通知
        try:
            msg = (
                f"[风险平仓-{symbol}] 平{side_cn}仓\n"
                f"{reason}\n"
                f"开仓价: {avg_px}, 当前价: {last_price}\n"
                f"浮动收益: {pnl_pct * 100:.2f}%"
            )
            send_wecom_text_func(msg)
        except Exception as e:
            print(f"[WARN] wecom notify failed: {e}")

        closed_any = True

    return closed_any


# ----------------------- 主逻辑 -----------------------


def run_once(cfg: Dict[str, Any]) -> None:
    interval = cfg.get("interval", "1h")
    bar = "1H"          # 交易级别
    htf_bar = "4H"      # 高周期过滤用
    print(f"Running bot once, interval={interval}, bar={bar}, htf_bar={htf_bar}")

    env = os.getenv("BOT_ENV", "test").lower()
    use_demo = env != "live"
    print(f"[ENV] BOT_ENV={env}, use_demo={use_demo}")

    trader = OKXTrader(cfg, use_demo=use_demo)

    for symbol in cfg.get("symbols", []):
        inst_id = symbol_to_inst_id(symbol)
        print(f"=== {symbol} / {inst_id} ===")

        # --- 获取 K 线数据 ---
        try:
            klines = trader.get_klines(inst_id, bar, 300)
            htf_klines = trader.get_klines(inst_id, htf_bar, 300)
            
            print(f"[DEBUG][KLINES] {symbol}: len(klines)={len(klines)}, len(htf_klines)={len(htf_klines)}")
            
        except Exception as e:
            print(f"[ERROR] fetch klines failed for {symbol}: {e}")
            continue

        # --- 生成策略信号 ---
        signal, info = generate_signal(
            symbol=symbol,
            klines=klines,
            cfg=cfg,
            htf_klines=htf_klines,
            debug=True,
        )
        print(f"[INFO] signal for {symbol}: {signal}, info: {info}")

        # --- 最新价格 ---
        try:
            last = trader.get_last_price(inst_id)
        except Exception as e:
            print(f"[ERROR] get_last_price failed for {symbol}: {e}")
            continue

        print(f"[INFO] last price {inst_id} = {last}")

        # --- 当前持仓 ---
        try:
            positions = trader.get_positions(inst_id)
        except Exception as e:
            print(f"[ERROR] get_positions failed for {symbol}: {e}")
            continue

        long_pos = pick_side_position(positions, "long")
        short_pos = pick_side_position(positions, "short")

        # --- 风险管理：先检查是否需要止盈/止损平仓 ---
        try:
            closed_by_risk = check_risk_close(
                symbol=symbol,
                inst_id=inst_id,
                last_price=last,
                positions=positions,
                cfg=cfg,
                trader=trader,
                send_wecom_text_func=send_wecom_text,
            )
        except Exception as e:
            print(f"[ERROR] risk check failed for {symbol}: {e}")
            closed_by_risk = False

        if closed_by_risk:
            print(
                f"[ACTION] {symbol}: position closed by risk rules, "
                f"skip new orders this round."
            )
            # 下一轮循环再重新看信号
            continue

        # ==========================
        # 下面是根据“策略信号”和“当前持仓”决定操作
        # ==========================

        # ---------- 做多信号 ----------
        if signal == 1:
            if long_pos:
                # 已经有多单，就不新增
                print("[ACTION] already long, no new long opened")
                continue

            # 如有空单，先平空再开多
            if short_pos:
                try:
                    sz = abs(float(short_pos.get("pos", "0")))
                except (TypeError, ValueError):
                    sz = 0
                if sz > 0:
                    print("[ACTION] close existing SHORT before opening LONG")
                    trader.close_short(inst_id, sz)

            print("Opening long ...")
            resp_open = trader.open_long(inst_id, last)
            print(f"open_long resp: {resp_open}")

            # 企业微信通知
            try:
                msg = (
                    f"[开多-{symbol}] 信号=多\n"
                    f"价格: {last}\n"
                    f"详情: {info}"
                )
                send_wecom_text(msg)
            except Exception as e:
                print(f"[WARN] wecom notify failed: {e}")

        # ---------- 做空信号 ----------
        elif signal == -1:
            if short_pos:
                print("[ACTION] already short, no new short opened")
                continue

            # 如有多单，先平多再开空
            if long_pos:
                try:
                    sz = abs(float(long_pos.get("pos", "0")))
                except (TypeError, ValueError):
                    sz = 0
                if sz > 0:
                    print("[ACTION] close existing LONG before opening SHORT")
                    trader.close_long(inst_id, sz)

            print("Opening short ...")
            resp_open = trader.open_short(inst_id, last)
            print(f"open_short resp: {resp_open}")

            # 企业微信通知
            try:
                msg = (
                    f"[开空-{symbol}] 信号=空\n"
                    f"价格: {last}\n"
                    f"详情: {info}"
                )
                send_wecom_text(msg)
            except Exception as e:
                print(f"[WARN] wecom notify failed: {e}")

        # ---------- 无信号：不操作 ----------
        else:
            print("[ACTION] no clear signal, do nothing.")

    print("Run once done.")


def main() -> None:
    cfg = load_config()
    run_once(cfg)


if __name__ == "__main__":
    main()
