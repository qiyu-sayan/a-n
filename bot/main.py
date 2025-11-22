# bot/main.py
import os
import sys
from datetime import datetime, timezone

from binance.client import Client
from binance.exceptions import BinanceAPIException, BinanceRequestException


def str2bool(v: str, default: bool = False) -> bool:
    if v is None:
        return default
    return v.strip().lower() in {"1", "true", "yes", "y", "on"}


def load_config():
    """
    从环境变量（GitHub Secrets）加载配置。
    """
    cfg = {}

    # 核心：你在 demo.binance.com 创建的 API Key
    cfg["api_key"] = os.getenv("BINANCE_KEY", "").strip()
    cfg["api_secret"] = os.getenv("BINANCE_SECRET", "").strip()

    # REST 接口地址：这里一定要用 api.binance.com，而不是 demo / testnet
    api_url = os.getenv("API_URL", "").strip()
    if not api_url:
        api_url = "https://api.binance.com"
    cfg["api_url"] = api_url

    # 交易开关
    cfg["enable_trading"] = str2bool(os.getenv("ENABLE_TRADING", "false"))
    cfg["paper_trading"] = str2bool(os.getenv("PAPER", "false"))

    # 每笔下单 USDT 金额（目前代码里不会自动下单，只是展示用）
    cfg["order_usdt"] = float(os.getenv("ORDER_USDT", "10"))

    # 风险相关参数（现在先不用，只是保留）
    cfg["risk_limit_usdt"] = float(os.getenv("RISK_LIMIT_USDT", "0") or 0)
    cfg["max_open_trades"] = int(os.getenv("MAX_OPEN_TRADES", "1") or 1)
    cfg["stop_loss_pct"] = float(os.getenv("STOP_LOSS_PCT", "2"))  # 例如 2%
    cfg["take_profit_pct"] = float(os.getenv("TAKE_PROFIT_PCT", "4"))  # 例如 4%
    cfg["slippage_bps"] = float(os.getenv("SLIPPAGE_BPS", "5"))  # 例如 5 bps = 0.05%

    # 交易标的，逗号分隔，例如：BTCUSDT,ETHUSDT
    symbols_raw = os.getenv("SYMBOLS", "BTCUSDT")
    cfg["symbols"] = [s.strip().upper() for s in symbols_raw.split(",") if s.strip()]

    # 是否标记为“测试网 / 模拟环境”仅用于打印
    # 你现在是 demo 盘，所以这里我们直接打印 DEMO。
    cfg["is_testnet_flag"] = os.getenv("BINANCE_TESTNET", "").strip()

    return cfg


def make_client():
    api_key = os.getenv("BINANCE_KEY")
    api_secret = os.getenv("BINANCE_SECRET")

    # GitHub secrets 中 API_URL 例如: https://testnet.binance.vision
    raw_api_url = os.getenv("API_URL", "https://testnet.binance.vision")

    # python-binance 需要 base_url 以 /api 结尾
    base_api_url = raw_api_url.rstrip("/") + "/api"

    client = Client(api_key, api_secret)
    client.API_URL = base_api_url

    print(f"REST API 地址: {client.API_URL}")
    return client


def print_header(cfg):
    """
    打印本次运行的配置概要。
    """
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S%z")
    print("📈 Bot 开始运行")
    print(f"时间: {now}")
    print(f"环境: DEMO (币安模拟盘 / demo.binance.com)")
    print(f"REST API 地址: {cfg['api_url']}")
    print(f"ENABLE_TRADING: {cfg['enable_trading']}")
    print(f"PAPER_TRADING: {cfg['paper_trading']}")
    print(f"每笔下单 USDT: {cfg['order_usdt']} （目前不会自动下单，仅作为预留参数）")
    print(f"交易标的: {', '.join(cfg['symbols'])}")
    print("-" * 60)


def handle_symbol(client: Client, cfg, symbol: str) -> str:
    """
    处理单个交易对：
    目前只：
      1. 获取最新价格
      2. 打印结果
      3. 不自动下单（后续再加策略）
    返回一个简短的结果字符串，用于最后汇总打印。
    """
    print(f"=== 处理交易对: {symbol} ===")
    try:
        ticker = client.get_symbol_ticker(symbol=symbol)
        price = float(ticker["price"])
        print(f"{symbol} 最新价格: {price:.4f}")

        # 这里可以以后加策略逻辑，例如：
        # signal = check_strategy(...)
        # if cfg['enable_trading'] and not cfg['paper_trading'] and signal == 'BUY':
        #     place_order(...)
        # 暂时仅输出说明，不实际下单。
        print("暂未启用自动下单逻辑，仅检查行情，跳过下单。")

        return f"{symbol}: 成功（本次未自动下单，仅检查行情）"

    except BinanceAPIException as e:
        print(f"❌ {symbol} 处理失败 - BinanceAPIException: {e.status_code} {e.message}")
        return f"{symbol}: 失败（BinanceAPIException {e.status_code}: {e.message})"
    except BinanceRequestException as e:
        print(f"❌ {symbol} 处理失败 - BinanceRequestException: {str(e)}")
        return f"{symbol}: 失败（BinanceRequestException: {str(e)})"
    except Exception as e:
        print(f"❌ {symbol} 处理失败 - 未知异常: {repr(e)}")
        return f"{symbol}: 失败（未知异常: {repr(e)})"


def run_bot():
    cfg = load_config()
    try:
        client = make_client(cfg)
    except Exception as e:
        print(f"❌ 初始化 Binance Client 失败: {repr(e)}")
        sys.exit(1)

    print_header(cfg)

    results = []
    for symbol in cfg["symbols"]:
        res = handle_symbol(client, cfg, symbol)
        results.append(res)
        print("-" * 60)

    print("📊 本次运行结果:")
    for line in results:
        print("  -", line)


if __name__ == "__main__":
    run_bot()
