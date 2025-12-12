import os
import sys
import traceback
from datetime import datetime

# 包内导入
from .trader import OKXTrader, load_config
from .strategy import generate_signal

# 根目录的企业微信推送
from wecom_notify import send_text as send_wecom_text


# ---------- 小工具 ----------

def symbol_to_inst_id(symbol: str) -> str:
    """
    把 BTCUSDT -> BTC-USDT-SWAP 这种 OKX 合约 instId
    之前我们也用过类似逻辑，这里在 main 里再实现一遍，避免导入问题。
    """
    symbol = symbol.upper()
    if symbol.endswith("USDT"):
        base = symbol[:-4]
        return f"{base}-USDT-SWAP"
    # 兜底：直接原样返回，方便调试
    return symbol


def notify_order(action: str,
                 symbol: str,
                 side: str,
                 price: float | None = None,
                 size: float | None = None,
                 extra: str | None = None) -> None:
    """
    企业微信下单 / 平仓 推送统一封装
    action: "开仓" / "平仓" / "风控平仓" / etc.
    side: "多" / "空"
    """
    try:
        ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
        lines = [
            f"🧠 交易机器人通知",
            f"操作：{action}",
            f"标的：{symbol}",
            f"方向：{side}",
        ]
        if size is not None:
            lines.append(f"合约张数：{size}")
        if price is not None:
            lines.append(f"参考价格：{price}")
        if extra:
            lines.append(extra)
        lines.append(f"时间：{ts}")

        msg = "\n".join(lines)
        send_wecom_text(msg)
    except Exception as e:
        # 推送失败不要影响交易本身
        print(f"[WECOM] send failed: {e}", file=sys.stderr)


# ---------- 主逻辑 ----------

def run_once(cfg: dict) -> None:
    """运行一次策略（对应 GitHub Actions 的一次 run-bot）"""

    # 1. 环境 & 交易对象初始化
    env = os.getenv("BOT_ENV", "test").lower()
    use_demo = env != "live"
    print(f"[ENV] BOT_ENV={env}, use_demo={use_demo}")

    interval = cfg.get("interval", "1h")
    bar = interval.upper()          # 1h -> 1H
    htf_bar = cfg.get("htf_bar", "4H")

    print(f"Running bot once, interval={interval}, bar={bar}, htf_bar={htf_bar}")

    trader = OKXTrader(cfg, use_demo=use_demo)

    risk_conf = cfg.get("risk", {})
    max_pos_pct = float(risk_conf.get("max_pos", 0.005))  # 最大单笔仓位占权益
    stop = float(risk_conf.get("stop", 0.05))             # 止损，例如 0.05 = -5%
    take = float(risk_conf.get("take", 0.10))             # 止盈，例如 0.10 = +10%

    # 2. 遍历每个交易品种
    for symbol in cfg.get("symbols", []):
        inst_id = symbol_to_inst_id(symbol)
        print(f"=== {symbol} / {inst_id} ===")

        # ---------- 2.1 风控检查：已有持仓先看要不要平 ----------
        risk_closed = False
        try:
            positions = trader.get_positions(inst_id)
        except Exception as e:
            print(f"[ERROR][RISK] get_positions failed for {symbol}: {e}")
            positions = []

        for pos in positions:
            pos_side = (pos.get("posSide") or "").lower()  # 'long' / 'short'
            sz_str = pos.get("pos") or "0"
            try:
                sz = float(sz_str)
            except ValueError:
                sz = 0.0

            if sz == 0:
                continue

            upl_ratio_raw = pos.get("uplRatio") or "0"
            try:
                pnl_pct = float(upl_ratio_raw)
            except ValueError:
                pnl_pct = 0.0

            print(
                f"[DEBUG][RISK] {symbol} {pos_side} pos={sz}, "
                f"pnl_pct={pnl_pct:.4f}, stop={-stop}, take={take}"
            )

            close_reason = None
            # uplRatio 通常是小数（0.05 = +5%），也有些返回百分比；这里假设是小数
            if pnl_pct <= -stop:
                close_reason = f"stop_loss {pnl_pct:.4f} <= -{stop}"
            elif pnl_pct >= take:
                close_reason = f"take_profit {pnl_pct:.4f} >= {take}"

            if close_reason:
                print(f"[ACTION][RISK] closing {pos_side.upper()} {symbol} due to {close_reason}")
                try:
                    if pos_side == "long":
                        trader.close_long(inst_id, sz)
                        side_cn = "多"
                    else:
                        trader.close_short(inst_id, sz)
                        side_cn = "空"

                    notify_order(
                        action="风控平仓",
                        symbol=symbol,
                        side=side_cn,
                        price=None,
                        size=sz,
                        extra=f"浮盈亏比例：{pnl_pct:.2%}\n原因：{close_reason}",
                    )
                except Exception as e:
                    print(f"[ERROR][RISK] close position failed for {symbol}: {e}")
                # 不管成功与否，本轮都不再对这个 symbol 开新仓
                risk_closed = True
                break

        if risk_closed:
            continue

        # ---------- 2.2 获取 K 线 ----------
        try:
            klines = trader.get_klines(inst_id, bar, 300)
            htf_klines = trader.get_klines(inst_id, htf_bar, 300)
            print(
                f"[DEBUG][KLINES] {symbol}: len(klines)={len(klines)}, "
                f"len(htf_klines)={len(htf_klines)}"
            )
        except Exception as e:
            print(f"[ERROR] fetch klines failed for {symbol}: {e}")
            continue

        if len(klines) < 50 or len(htf_klines) < 50:
            print(
                f"[INFO] signal for {symbol}: 0, "
                f"info={{'symbol': '{symbol}', 'reason': 'not_enough_data'}}"
            )
            print("[ACTION] no clear signal, do nothing.")
            continue

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
            traceback.print_exc()
            continue

        print(f"[INFO] signal for {symbol}: {signal}, info: {info}")

        # ---------- 2.4 查询当前持仓状态 ----------
        try:
            positions = trader.get_positions(inst_id)
        except Exception as e:
            print(f"[ERROR] get_positions failed for {symbol}: {e}")
            positions = []

        long_sz = 0.0
        short_sz = 0.0
        for pos in positions:
            side = (pos.get("posSide") or "").lower()
            try:
                sz = float(pos.get("pos") or "0")
            except ValueError:
                sz = 0.0
            if side == "long":
                long_sz += sz
            elif side == "short":
                short_sz += sz

        has_long = long_sz > 0
        has_short = short_sz > 0

        # ---------- 2.5 根据信号执行交易 ----------
        try:
            last = trader.get_last_price(inst_id)
            print(f"[INFO] last price {inst_id} = {last}")
        except Exception as e:
            print(f"[ERROR] get_last_price failed for {symbol}: {e}")
            last = None

        # signal: -1 -> 做空, 1 -> 做多, 0 -> 不操作
        if signal == 0:
            print("[ACTION] no clear signal, do nothing.")
            continue

        # 先处理“反向平仓”的情况
        if signal == 1 and has_short:
            print("[ACTION] close existing SHORT before opening LONG")
            try:
                trader.close_short(inst_id, short_sz)
                notify_order(
                    action="平空",
                    symbol=symbol,
                    side="空",
                    price=last,
                    size=short_sz,
                    extra="信号反转，平空准备做多",
                )
            except Exception as e:
                print(f"[ERROR] close_short failed for {symbol}: {e}")

        if signal == -1 and has_long:
            print("[ACTION] close existing LONG before opening SHORT")
            try:
                trader.close_long(inst_id, long_sz)
                notify_order(
                    action="平多",
                    symbol=symbol,
                    side="多",
                    price=last,
                    size=long_sz,
                    extra="信号反转，平多准备做空",
                )
            except Exception as e:
                print(f"[ERROR] close_long failed for {symbol}: {e}")

        # 再根据信号决定是否开新仓
        if signal == 1:
            if has_long and not has_short:
                print("[ACTION] already long, no new long opened")
            else:
                print("Opening long ...")
                try:
                    # 不传 size，交给 OKXTrader 里根据 max_pos_pct 自动算
                    resp = trader.open_long(inst_id, ref_price=last,
                                            max_pos_pct=max_pos_pct)
                    print(f"[DEBUG] open_long resp: {resp}")
                    notify_order(
                        action="开多",
                        symbol=symbol,
                        side="多",
                        price=last,
                        size=None,
                    )
                except Exception as e:
                    print(f"[ERROR] open_long failed for {symbol}: {e}")

        elif signal == -1:
            if has_short and not has_long:
                print("[ACTION] already short, no new short opened")
            else:
                print("Opening short ...")
                try:
                    resp = trader.open_short(inst_id, ref_price=last,
                                             max_pos_pct=max_pos_pct)
                    print(f"[DEBUG] open_short resp: {resp}")
                    notify_order(
                        action="开空",
                        symbol=symbol,
                        side="空",
                        price=last,
                        size=None,
                    )
                except Exception as e:
                    print(f"[ERROR] open_short failed for {symbol}: {e}")

    print("Run once done.")


def main() -> None:
    cfg = load_config()
    run_once(cfg)


if __name__ == "__main__":
    main()
