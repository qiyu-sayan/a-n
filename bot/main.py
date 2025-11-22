#!/usr/bin/env python
# -*- coding: utf-8 -*-

import os
import sys
import traceback
from datetime import datetime
from typing import List

from binance.client import Client
from binance.exceptions import BinanceAPIException, BinanceRequestException


# ========= 辅助函数 =========

def str2bool(val: str, default: bool = False) -> bool:
    if val is None:
        return default
    return str(val).strip().lower() in ("1", "true", "yes", "y", "on")


def safe_float(val: str, default: float = 0.0) -> float:
    if val is None or val == "":
        return default
    try:
        return float(val)
    except Exception:
        raise RuntimeError(f"环境变量不是数字: {val!r}")


# 尝试安全地调用 wecom_notify
def safe_wecom_notify(text: str) -> None:
    webhook = os.getenv("WECHAT_WEBHOOK", "").strip()
    if not webhook:
        # 没配置 webhook 就直接跳过
        return
    try:
        from wecom_notify import wecom_notify
    except Exception:
        # 没有这个模块/函数就静默忽略
        return

    try:
        # 优先按“有参数”的方式调用
        wecom_notify(text)
    except TypeError:
        # 如果原函数不需要参数，再尝试无参调用
        try:
            wecom_notify()
        except Exception:
            pass
    except Exception:
        # 其他异常直接忽略，避免影响交易逻辑
        pass


# ========= Binance 客户端 =========



def make_client():
    """
    创建 Binance Client，并返回 (client, raw_api_url, base_api_url)

    - 优先使用 GitHub Secrets 里的 API_URL
    - 如果 API_URL 没填，就默认用 https://api.binance.com
    - 通过 client.API_URL 来设置接口地址（兼容旧版 python-binance）
    """
    api_key = os.getenv("BINANCE_KEY", "").strip()
    api_secret = os.getenv("BINANCE_SECRET", "").strip()

    if not api_key or not api_secret:
        raise RuntimeError("BINANCE_KEY / BINANCE_SECRET 没配置好，无法创建客户端")

    raw_api_url = os.getenv("API_URL", "").strip()

    if not raw_api_url:
        # 没配就用官方 REST 地址（demo 的 API 也是走这个）
        base_api_url = "https://api.binance.com"
        raw_api_url = base_api_url
    else:
        # 用户自己配了，就规范一下：
        # 1. 已经带 http:// 或 https:// 的，直接用
        # 2. 没带协议的，只加一个 https:// 前缀，不再乱加 api 等字符串
        if "://" in raw_api_url:
            base_api_url = raw_api_url
        else:
            base_api_url = "https://" + raw_api_url

    # 兼容旧版 python-binance：不能传 base_url，只能事后改 API_URL
    client = Client(api_key, api_secret)
    client.API_URL = base_api_url

    # 先 ping 一下，确认能连上
    client.ping()

    return client, raw_api_url, base_api_url


# ========= 策略占位（当前只看行情，不下单） =========

def load_symbols() -> List[str]:
    raw = os.getenv("SYMBOLS", "BTCUSDT")
    symbols = [s.strip().upper() for s in raw.split(",") if s.strip()]
    uniq = []
    for s in symbols:
        if s not in uniq:
            uniq.append(s)
    return uniq or ["BTCUSDT"]


def run_bot() -> bool:
    client, raw_api_url, base_api_url = make_client()

    enable_trading = str2bool(os.getenv("ENABLE_TRADING", "false"))
    paper_trading = str2bool(os.getenv("PAPER", "true"))
    order_usdt = safe_float(os.getenv("ORDER_USDT", "10.0"), 10.0)

    symbols = load_symbols()

    # 环境识别（纯展示用）
    env_label = "REAL"
    url_lower = raw_api_url.lower()
    if "testnet" in url_lower:
        env_label = "TESTNET(旧测试网 / testnet.binance.vision)"
    elif "api.binance.com" in url_lower:
        env_label = "DEMO(币安模拟盘 / demo.binance.com，用正式 API 域名)"

    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S+0000")

    header_lines = [
        "📈 Bot 开始运行",
        f"时间: {now}",
        f"环境: {env_label}",
        f"REST API 地址: {base_api_url}",
        "",
        f"ENABLE_TRADING: {enable_trading}",
        f"PAPER_TRADING: {paper_trading}",
        f"每笔下单 USDT: {order_usdt}  (当前阶段不会自动下单，仅作为预留参数)",
        f"交易标的: {', '.join(symbols)}",
        "-" * 60,
    ]

    for line in header_lines:
        print(line)

    per_symbol_results = []
    overall_ok = True

    for symbol in symbols:
        print(f"=== 处理交易对: {symbol} ===")
        try:
            ticker = client.get_symbol_ticker(symbol=symbol)
            price = float(ticker["price"])
            print(f"{symbol} 最新价格: {price:.6f}")

            # 这里是策略占位：当前只打印价格，不做买卖
            print(f"{symbol}: 当前阶段仅检查行情，不自动下单。")

            per_symbol_results.append(f"- {symbol}: 成功（仅检查行情，未下单）")

        except (BinanceAPIException, BinanceRequestException) as e:
            overall_ok = False
            print(f"❌ {symbol} 处理失败 - {type(e).__name__}: {e}")
            per_symbol_results.append(f"- {symbol}: 失败（{type(e).__name__}: {e}）")
        except Exception as e:
            overall_ok = False
            print(f"❌ {symbol} 处理失败 - 未知异常: {e}")
            traceback.print_exc()
            per_symbol_results.append(f"- {symbol}: 失败（未知异常: {e}）")

        print("-" * 60)

    summary_lines = ["📊 本次运行结果:"]
    summary_lines.extend(per_symbol_results)

    summary = "\n".join(summary_lines)
    print(summary)

    # WeCom 推送（如果配置了 WECHAT_WEBHOOK）
    try:
        safe_wecom_notify(summary)
    except Exception:
        pass

    # 这里不再 sys.exit(1)，而是把结果返回给上层
    return overall_ok


if __name__ == "__main__":
    try:
        ok = run_bot()
        # 即使 ok 为 False，我们也不退出 1，只是在控制台里能看到哪些币种失败。
        # 如果你以后想让 “有失败就标红”，可以在这里再加一行:
        # if not ok: sys.exit(1)
    except Exception as e:
        # 真正脚本级别的致命错误，才退出 1
        err_text = f"run-bot 发生致命异常: {e}\n{traceback.format_exc()}"
        print(err_text)
        try:
            safe_wecom_notify(err_text[:1500])
        except Exception:
            pass
        sys.exit(1)
