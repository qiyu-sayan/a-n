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


def symbol_to_inst_id(symbol: str) -> str:
    """
    把 BTCUSDT -> BTC-USDT-SWAP
       ETHUSDT -> ETH-USDT-SWAP
    """
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
    # OKX 返回是最新在前，这里反转一下方便做指标
    klines.reverse()
    return klines


def interval_to_bar(interval: str) -> str:
    """
    把配置里的 interval 映射成 OKX 的 bar 参数。
    例如：
        "1h" -> "1H"
        "4h" -> "4H"
        "15m" -> "15m"
    """
    if not interval:
        return "1H"
    # 简单粗暴：h 变大写 H，其他保持不动
    return interval.replace("h", "H")


def run_once(cfg: Dict[str, Any]) -> None:
    """
    跑一轮：对每个 symbol 做一次信号判断和下单。
    由 GitHub Actions 定时唤醒，所以这里只跑一轮即可。
    """
    trader = OKXTrader(cfg, use_demo=True)  # 现在先默认模拟盘

    base_url = trader.base_url  # https://www.okx.com 或模拟盘地址
    symbols = cfg.get("symbols", ["BTCUSDT", "ETHUSDT"])
    interval = cfg.get("interval", "1h")
    bar = interval_to_bar(interval)

    # 高周期（趋势）用 4h，可以以后做成配置
    htf_bar = "4H"

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

        # 不发新信号就跳过
        if signal == 0:
            continue

        # 下单前先获取一眼价格，仅用于日志（真正下单时 trader 会自己取最新价）
        try:
            last = trader.get_last_price(inst_id)
        except Exception:
            last = None
        print(f"[INFO] last price {inst_id} = {last}")

        # 简单的“净仓”风格：
        # - 做多信号：先平空，再开多
        # - 做空信号：先平多，再开空
        try:
            if signal == 1:
                print("[ACTION] close_short then open_long")
                try:
                    trader.close_short(inst_id)
                except Exception as e:
                    print(f"[WARN] close_short failed (maybe no short position): {e}")
                trader.open_long(inst_id, ref_price=last)

            elif signal == -1:
                print("[ACTION] close_long then open_short")
                try:
                    trader.close_long(inst_id)
                except Exception as e:
                    print(f"[WARN] close_long failed (maybe no long position): {e}")
                trader.open_short(inst_id, ref_price=last)

        except Exception as e:
            print(f"[ERROR] trade action failed for {inst_id}: {e}")

        # 给接口留一点喘息时间
        time.sleep(0.5)


def main():
    cfg = load_config()
    run_once(cfg)


if __name__ == "__main__":
    main()
