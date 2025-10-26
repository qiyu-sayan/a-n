import os
import time
import json
import logging
import requests
from dotenv import load_dotenv
from binance.spot import Spot as Client

# ========= 环境 =========
load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("logs/latest.log", encoding="utf-8"),
        logging.StreamHandler()
    ]
)
log = logging.getLogger(__name__)

# 运行参数
ENABLE_TRADING = os.getenv("ENABLE_TRADING", "0") == "1"
PAPER          = os.getenv("PAPER", "1") == "1"
ORDER_USDT     = float(os.getenv("ORDER_USDT", "10"))
SYMBOLS        = [s.strip() for s in os.getenv("SYMBOLS", "BTCUSDT,ETHUSDT").split(",") if s.strip()]
WECHAT_HOOK    = os.getenv("WECHAT_WEBHOOK", "")

# ========= 客户端 =========
def make_client():
    api_key = os.getenv("BINANCE_KEY")
    api_secret = os.getenv("BINANCE_SECRET")
    testnet = os.getenv("BINANCE_TESTNET", "0") == "1"
    if testnet:
        base_url = "https://testnet.binance.vision"
        log.info("Using Binance Testnet ✅")
    else:
        base_url = None
        log.info("Using Binance Mainnet 🌍")
    return Client(key=api_key, secret=api_secret, base_url=base_url)

client = make_client()

# ========= 工具 =========
_warned_451 = set()

def is_451(e: Exception) -> bool:
    msg = str(e).lower()
    return ("451" in msg) or ("restricted location" in msg) or ("eligibility" in msg)

def wechat(text: str):
    if not WECHAT_HOOK:
        return
    try:
        requests.post(WECHAT_HOOK, json={"msgtype": "text", "text": {"content": text}}, timeout=8)
    except Exception as ex:
        log.warning(f"WeCom push failed: {ex}")

def fetch_klines_safe(symbol: str, interval="1m", limit=200, max_retries=2):
    backoff = 1.5
    for i in range(max_retries + 1):
        try:
            return client.klines(symbol, interval=interval, limit=limit)
        except Exception as e:
            if is_451(e):
                if symbol not in _warned_451:
                    _warned_451.add(symbol)
                    log.warning(f"[{symbol}] 451/地区限制，已跳过该标的。")
                    wechat(f"[跳过]{symbol} 451/地区限制")
                return None
            if i < max_retries:
                sleep_s = backoff ** i
                log.warning(f"[{symbol}] 拉K线异常：{e}，{sleep_s:.1f}s后重试（{i+1}/{max_retries}）")
                time.sleep(sleep_s)
            else:
                raise

def last_price(symbol: str) -> float:
    # 用 klines 的收盘价即可，省一次请求
    k = fetch_klines_safe(symbol, "1m", 2)
    if not k:
        return None
    return float(k[-1][4])

# ========= 简单信号（示例：收盘价 > 简单均线）=========
def simple_signal(symbol: str):
    k = fetch_klines_safe(symbol, "1m", 50)
    if not k:
        return None
    closes = [float(x[4]) for x in k]
    price  = closes[-1]
    sma20  = sum(closes[-20:]) / 20.0
    log.info(f"[{symbol}] price={price:.2f}  sma20={sma20:.2f}")
    if price > sma20 * 1.001:   # 略高于均线才触发，避免来回抖动
        return {"side": "BUY", "price": price}
    elif price < sma20 * 0.999:
        return {"side": "SELL", "price": price}
    return None

# ========= 下单（含纸交易/测试单/强制安全护栏）=========
def place_order(symbol: str, side: str, usdt_notional: float):
    """
    - 如果 PAPER==True：优先 new_order_test（测试单，不落真实委托）
    - 如果 ENABLE_TRADING==False：只打印，不下单
    - 真下单严格要求：ENABLE_TRADING=True 且 PAPER=False 且 非测试网
    """
    price = last_price(symbol)
    if price is None or price <= 0:
        log.warning(f"[{symbol}] 无法获取价格，跳过下单")
        return {"status": "skip", "reason": "no_price"}

    qty = round(usdt_notional / price, 6)  # 简单按市价名义金额换算数量，精度6位
    log.info(f"[{symbol}] 准备下单 side={side} qty≈{qty} (by {usdt_notional} USDT)")

    # 纸交易 or 未开启交易 -> 只走测试单/打印
    if PAPER or not ENABLE_TRADING:
        try:
            client.new_order_test(symbol, side, "MARKET", quantity=qty)
            log.info(f"[PAPER] {symbol} {side} 测试单已提交（不会成交落单）")
            wechat(f"[PAPER] {symbol} {side} qty≈{qty}")
            return {"status": "paper_ok", "qty": qty}
        except Exception as e:
            log.warning(f"[PAPER] 提交测试单失败：{e}")
            wechat(f"[PAPER.FAIL] {symbol} {side} -> {e}")
            return {"status": "paper_fail", "error": str(e)}

    # 真仓（强保护：只有显式打开才可能走到这里）
    try:
        res = client.new_order(symbol, side, "MARKET", quantity=qty)
        log.info(f"[LIVE] 下单成功：{json.dumps(res)[:300]}...")
        wechat(f"[LIVE] {symbol} {side} qty≈{qty}")
        return {"status": "live_ok", "qty": qty, "resp": res}
    except Exception as e:
        log.error(f"[LIVE] 下单失败：{e}")
        wechat(f"[LIVE.FAIL] {symbol} {side} -> {e}")
        return {"status": "live_fail", "error": str(e)}

# ========= 主流程 =========
def main():
    log.info("🚀 run start")
    wechat("▶️ run start")
    log.info(f"SYMBOLS={SYMBOLS}  ENABLE_TRADING={ENABLE_TRADING}  PAPER={PAPER}  ORDER_USDT={ORDER_USDT}")

    acted = 0
    for sym in SYMBOLS:
        sig = simple_signal(sym)
        if not sig:
            continue
        # 演示：拿到信号就试下单（默认 PAPER=1，只走测试）
        res = place_order(sym, sig["side"], ORDER_USDT)
        acted += 1
        time.sleep(0.2)
wechat("✅ run end")
    if acted == 0:
        log.info("本次无交易动作（可能无信号或被 451 跳过）。")
    
    log.info("🏁 run end")

if __name__ == "__main__":
    main()