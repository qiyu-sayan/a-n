"""
main.py

职责：
- 读取配置
- 为每个交易对获取 K 线数据
- 调用 strategy.generate_signal 生成信号
- 调用 OKXTrader 在 OKX 上开仓 / 平仓

运行方式：
    python -m bot.main
由 GitHub Actions 的 run-bot workflow 定时触发。
"""

import time
from typing import Dict, Any, List, Tuple

import requests

from bot.trader import OKXTrader, load_config
from bot.strategy import generate_signal
# 尝试导入企业微信推送函数，如果没有就静默忽略
try:
    from wecom_notify import send_wecom_text  # 如果你以前的函数名不是这个，下面我会给兼容写法
except Exception:
    send_wecom_text = None


def symbol_to_inst_id(symbol: str) -> str:
    """
    把 BTCUSDT -> BTC-USDT-SWAP
       ETHUSDT -> ETH-USDT-SWAP
       BNBUSDT -> BNB-USDT-SWAP
       ASTERUSDT -> ASTER-USDT-SWAP
    """
    symbol = symbol.upper()
    if symbol.endswith("USDT"):
        base = symbol[:-4]
    else:
        base = symbol
    return f"{base}-USDT-SWAP"


def fetch_klines(
    base_url: str,
    inst_id: str,
    bar: str,
    limit: int = 200,
) -> List[List[Any]]:
    """
    从 OKX 公共接口获取 K 线数据。
    返回按时间从旧 -> 新排序的列表。
    """
    url = base_url + "/api/v5/market/candles"
    params = {
        "instId": inst_id,
        "bar": bar,
        "limit": str(limit),
    }
    resp = requests.get(url, params=params, timeout=10)
    data = resp.json()
    if data.get("code") != "0":
        raise RuntimeError(f"fetch_klines error: {data}")
    klines = data.get("data", [])
    klines.reverse()
    return klines


def interval_to_bar(interval: str) -> str:
    if not interval:
        return "1H"
    return interval.replace("h", "H")


def run_once(cfg: Dict[str, Any]) -> None:
    """
    跑一轮：对每个 symbol 做一次信号判断和下单。
    由 GitHub Actions 定时唤醒，所以这里只跑一轮即可。
    """
    trader = OKXTrader(cfg, use_demo=True)  # 目前默认模拟盘

    base_url = trader.base_url
    symbols = cfg.get("symbols", ["BTCUSDT", "ETHUSDT"])
    interval = cfg.get("interval", "1h")
    bar = interval_to_bar(interval)
    htf_bar = "4H"  # 高周期

    print(f"Running bot once, interval={interval}, bar={bar}, htf_bar={htf_bar}")
    for symbol in symbols:
        inst_id = symbol_to_inst_id(symbol)
        print(f"\n=== {symbol} / {inst_id} ===")

        try:
            klines = fetch_klines(base_url, inst_id, bar, limit=200)
            htf_klines = fetch_klines(base_url, inst_id, htf_bar, limit=200)
        except Exception as e:
            print(f"[ERROR] fetch_klines failed for {inst_id}: {e}")
            continue

        signal, info = generate_signal(
            symbol=symbol,
            klines=klines,
            cfg=cfg,
            htf_klines=htf_klines,
            debug=True,
        )

        print(f"[INFO] signal for {symbol}: {signal}, info: {info}")

        if signal == 0:
            continue

        # 当前仓位情况
        has_long = False
        has_short = False
        try:
            positions = trader.get_positions(inst_id)
            for p in positions:
                try:
                    pos = float(p.get("pos", "0"))
                    side = p.get("posSide")
                    if pos > 0:
                        if side == "long":
                            has_long = True
                        elif side == "short":
                            has_short = True
                except Exception:
                    continue
        except Exception as e:
            print(f"[WARN] get_positions failed for {inst_id}: {e}")

        # 仅当 signal 与当前持仓方向“不一致”时才动作
        try:
            last = trader.get_last_price(inst_id)
        except Exception:
            last = None
                print(f"[INFO] last price {inst_id} = {last}")

        try:
            if signal == 1:
                if has_long:
                    print("[ACTION] already long, no new long opened")
                else:
                    action_desc = "close_short_then_open_long" if has_short else "open_long"
                    if has_short:
                        print("[ACTION] close_short then open_long")
                        try:
                            trader.close_short(inst_id)
                        except Exception as e:
                            print(f"[WARN] close_short failed: {e}")
                    else:
                        print("[ACTION] open_long (no existing position)")

                    trader.open_long(inst_id, ref_price=last)

                    # === 企业微信推送（做多开仓成功后） ===
                    if send_wecom_text is not None:
                        msg = (
                            f"[开多] {symbol} / {inst_id}\n"
                            f"价格: {last}\n"
                            f"信号: {info}\n"
                            f"动作: {action_desc}"
                        )
                        try:
                            send_wecom_text(msg)
                        except Exception as e:
                            print(f"[WARN] send_wecom_text failed: {e}")

            elif signal == -1:
                if has_short:
                    print("[ACTION] already short, no new short opened")
                else:
                    action_desc = "close_long_then_open_short" if has_long else "open_short"
                    if has_long:
                        print("[ACTION] close_long then open_short")
                        try:
                            trader.close_long(inst_id)
                        except Exception as e:
                            print(f"[WARN] close_long failed: {e}")
                    else:
                        print("[ACTION] open_short (no existing position)")

                    trader.open_short(inst_id, ref_price=last)

                    # === 企业微信推送（做空开仓成功后） ===
                    if send_wecom_text is not None:
                        msg = (
                            f"[开空] {symbol} / {inst_id}\n"
                            f"价格: {last}\n"
                            f"信号: {info}\n"
                            f"动作: {action_desc}"
                        )
                        try:
                            send_wecom_text(msg)
                        except Exception as e:
                            print(f"[WARN] send_wecom_text failed: {e}")

        except Exception as e:
            print(f"[ERROR] trade action failed for {inst_id}: {e}")

        time.sleep(0.5)


def main():
    cfg = load_config()
    run_once(cfg)


if __name__ == "__main__":
    main()
