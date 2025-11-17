import os
import json
import traceback
from datetime import datetime, timezone

import ccxt
import urllib.request


CONFIG_PATH = "config/params.json"


# ========= 企业微信发送 =========
def send_wecom(text: str) -> None:
    """
    直接用 webhook 给企业微信发文本消息。
    使用仓库 Secrets 里的 WECHAT_WEBHOOK（你现在就在用的那个）。
    """
    webhook = os.getenv("WECHAT_WEBHOOK", "").strip()
    if not webhook:
        print("[wecom] WECHAT_WEBHOOK 未配置，跳过发送：")
        print(text)
        return

    payload = {
        "msgtype": "text",
        "text": {
            "content": text,
        },
    }

    try:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            webhook,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310
            resp_body = resp.read().decode("utf-8")
        print("[wecom] 已发送：", resp_body)
    except Exception as e:  # noqa: BLE001
        print("[wecom] 发送失败：", e)
        print("内容：", text)


# ========= 配置 & 工具 =========
def load_config() -> dict:
    """从 config/params.json 里读一些默认配置，没有就用空字典。"""
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        print("[main] params.json not found, using defaults")
        return {}
    except json.JSONDecodeError as e:
        print(f"[main] params.json JSON 解析错误: {e}")
        return {}


def str2bool(s: str, default: bool = False) -> bool:
    if s is None:
        return default
    return str(s).strip().lower() in ("1", "true", "yes", "y", "on")


def normalize_symbol(sym: str) -> str:
    """
    尽量兼容两种写法：
    - 'BTCUSDT'  -> 'BTC/USDT'
    - 'BTC/USDT' -> 'BTC/USDT'
    """
    sym = sym.strip().upper()
    if "/" in sym:
        return sym
    if sym.endswith("USDT"):
        return sym[:-4] + "/USDT"
    return sym


def parse_symbols(cfg: dict) -> list[str]:
    """
    优先用环境变量 SYMBOLS，其次用 params.json 里的 symbols，最后默认 BTC/USDT。
    - SYMBOLS 可以是：'BTCUSDT,ETHUSDT' 或 '["BTCUSDT","ETHUSDT"]'
    """
    env_symbols = os.getenv("SYMBOLS", "").strip()
    symbols: list[str] | None = None

    if env_symbols:
        if env_symbols.startswith("["):
            # JSON 格式
            try:
                arr = json.loads(env_symbols)
                if isinstance(arr, list):
                    symbols = [str(x) for x in arr]
            except json.JSONDecodeError:
                pass
        if symbols is None:
            # 逗号分隔格式
            symbols = [s for s in env_symbols.split(",") if s.strip()]

    if not symbols:
        cfg_symbols = cfg.get("symbols") or cfg.get("SYMBOLS")
        if isinstance(cfg_symbols, list) and cfg_symbols:
            symbols = [str(x) for x in cfg_symbols]

    if not symbols:
        symbols = ["BTCUSDT"]

    return [normalize_symbol(s) for s in symbols]


def make_exchange():
    api_key = os.getenv("BINANCE_KEY")
    secret = os.getenv("BINANCE_SECRET")
    is_testnet = str2bool(os.getenv("BINANCE_TESTNET", "true"), True)

    if not api_key or not secret:
        raise RuntimeError("BINANCE_KEY / BINANCE_SECRET 没有设置，无法下单")

    exchange = ccxt.binance(
        {
            "apiKey": api_key,
            "secret": secret,
            "enableRateLimit": True,
            "options": {"defaultType": "spot"},
        }
    )

    # ccxt 的测试网开关
    if is_testnet:
        exchange.set_sandbox_mode(True)

    return exchange, is_testnet


# ========= 主交易逻辑 =========
def run_bot():
    cfg = load_config()

    enable_trading = str2bool(os.getenv("ENABLE_TRADING", "false"), False)
    paper = str2bool(os.getenv("PAPER", "true"), True)
    order_usdt_str = os.getenv("ORDER_USDT", "10")

    try:
        order_usdt = float(order_usdt_str)
    except ValueError:
        order_usdt = 10.0

    symbols = parse_symbols(cfg)

    exchange, is_testnet = make_exchange()

    head = [
        "🚀 Bot 开始运行",
        f"时间: {datetime.now(timezone.utc).astimezone().isoformat()}",
        f"环境: {'TESTNET(模拟盘)' if is_testnet else 'LIVE(实盘)'}",
        f"ENABLE_TRADING: {enable_trading}",
        f"PAPER(纸上仿真): {paper}",
        f"每笔下单 USDT: {order_usdt}",
        f"交易标的: {', '.join(symbols)}",
    ]
    head_msg = "\n".join(head)
    print(head_msg)
    send_wecom(head_msg)

    if not enable_trading:
        msg = "ENABLE_TRADING = false，本次只做连通性测试，不下单。"
        print(msg)
        send_wecom(msg)
        return

    results: list[str] = []

    for sym in symbols:
        try:
            ticker = exchange.fetch_ticker(sym)
            last = ticker.get("last") or ticker.get("close")
            if not last:
                results.append(f"{sym}: 获取价格失败，跳过。")
                continue

            amount = order_usdt / float(last)

            if paper:
                line = f"[PAPER] {sym}: 价格约 {last:.4f}，理论买入数量 {amount:.6f}"
                print(line)
                results.append(line)
            else:
                order = exchange.create_market_buy_order(sym, amount)
                line = f"[REAL] {sym}: 市价买入 {amount:.6f}，订单ID: {order.get('id')}"
                print(line)
                results.append(line)

        except Exception as e:  # noqa: BLE001
            err = f"{sym}: 下单失败 - {e}"
            print(err)
            results.append(err)

    summary = "本次运行结果：\n" + "\n".join(results)
    send_wecom(summary)


def main():
    try:
        run_bot()
        send_wecom("✅ 本次 run-bot 任务执行完毕")
    except Exception as e:  # noqa: BLE001
        tb = traceback.format_exc()
        print(tb)
        send_wecom(f"❌ run-bot 发生异常: {e}\n\n{tb[:1500]}")


if __name__ == "__main__":
    main()
