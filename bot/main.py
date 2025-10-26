import os
import time
import logging
from dotenv import load_dotenv
from binance.spot import Spot as Client

# ========== 环境初始化 ==========
load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("logs/latest.log", encoding="utf-8"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# ========== Binance 客户端创建 ==========
def make_client():
    api_key = os.getenv("BINANCE_KEY")
    api_secret = os.getenv("BINANCE_SECRET")
    testnet = os.getenv("BINANCE_TESTNET", "0") == "1"

    if testnet:
        base_url = "https://testnet.binance.vision"
        logger.info("Using Binance Testnet environment ✅")
    else:
        base_url = None
        logger.info("Using Binance Mainnet environment 🌍")

    return Client(key=api_key, secret=api_secret, base_url=base_url)

client = make_client()

# ========== 防止 451 错误重复刷屏 ==========
_warned_451_once = set()

def _is_region_restricted_error(e: Exception) -> bool:
    msg = str(e).lower()
    return ("451" in msg) or ("restricted location" in msg) or ("eligibility" in msg)

def warn_once(key: str, text: str):
    if key not in _warned_451_once:
        _warned_451_once.add(key)
        logger.warning(text)

# ========== 取 K 线函数（封装好异常处理） ==========
def fetch_klines_safe(symbol: str, interval="1m", limit=200, max_retries=2):
    backoff = 1.5
    for i in range(max_retries + 1):
        try:
            return client.klines(symbol, interval=interval, limit=limit)
        except Exception as e:
            if _is_region_restricted_error(e):
                warn_once(
                    f"451:{symbol}",
                    f"[{symbol}] 访问被限制(451/eligibility)，已跳过该标的，不影响其他标的运行。"
                )
                return None  # 软跳过
            # 其他错误轻量重试
            if i < max_retries:
                sleep_s = backoff ** i
                logger.warning(f"[{symbol}] 拉取K线异常：{e}，{sleep_s:.1f}s后重试（{i+1}/{max_retries}）")
                time.sleep(sleep_s)
            else:
                raise

# ========== 主逻辑 ==========
def analyze_symbol(symbol):
    klines = fetch_klines_safe(symbol)
    if klines is None:
        return None

    closes = [float(x[4]) for x in klines]
    avg_price = sum(closes) / len(closes)
    logger.info(f"[{symbol}] 最新价: {closes[-1]:.2f}, 平均价: {avg_price:.2f}")
    return {
        "symbol": symbol,
        "price": closes[-1],
        "avg": avg_price
    }

def main():
    logger.info("🚀 Bot run start")
    symbols = ["BTCUSDT", "ETHUSDT"]
    results = []

    for sym in symbols:
        res = analyze_symbol(sym)
        if res:
            results.append(res)

    if not results:
        logger.warning("❌ 无有效交易对结果，可能是地区限制或接口返回空。")
    else:
        logger.info(f"✅ 本次分析完成，共 {len(results)} 个标的。")

    logger.info("🏁 Bot run end")

if __name__ == "__main__":
    main()