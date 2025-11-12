# trainer/train.py
import os, sys, json, random, time
from datetime import datetime

# --- 可选：企业微信告警（没有也不影响运行） ---
try:
    from bot.wecom_notify import wecom_notify, warn_451  # 你仓库里有的话就会用到
except Exception:
    def wecom_notify(*args, **kwargs):
        pass
    def warn_451(*args, **kwargs):
        pass


def load_cfg():
    """
    合并优先级：ENV > params.json > 默认
    环境变量：
      SYMBOLS：逗号分隔，如  BTCUSDT,ETHUSDT
      INTERVAL：如 1h / 15m
    """
    # 1) 读文件
    file_cfg = {}
    try:
        with open("config/params.json", "rb") as f:
            file_cfg = json.load(f)
    except FileNotFoundError:
        file_cfg = {}

    # 2) 环境变量覆盖
    env_symbols = os.getenv("SYMBOLS", "").strip()
    if env_symbols:
        symbols = [s.strip().upper() for s in env_symbols.split(",") if s.strip()]
    else:
        symbols = file_cfg.get("symbols")

    interval = os.getenv("INTERVAL", "").strip() or file_cfg.get("interval") or "1h"
    risk     = file_cfg.get("risk")  or {"max_pos": 0.3, "stop": 0.02, "take": 0.04}
    logic    = file_cfg.get("logic") or {"fast": 12, "slow": 26, "sig": 9}

    if not symbols:
        print("ERROR: symbols 为空。请设置 Secrets.SYMBOLS（示例：BTCUSDT,ETHUSDT），"
              "或在 config/params.json 写入 symbols 数组。", file=sys.stderr)
        sys.exit(1)

    return {
        "symbols": symbols,
        "interval": interval,
        "risk": risk,
        "logic": logic,
    }


def walk_forward_backtest(symbols, interval, risk, lookback_hours=24, logic=None):
    """
    示例占位：这里替换成你的真实回测逻辑即可。
    现在仅生成一个可重复的随机结果，演示参数更新流程。
    """
    seed = hash((tuple(symbols), interval, json.dumps(risk, sort_keys=True), json.dumps(logic or {}, sort_keys=True))) & 0xffffffff
    rnd = random.Random(seed)
    sharpe = round(rnd.uniform(-0.5, 2.0), 3)
    winrate = round(rnd.uniform(0.35, 0.7), 3)
    pnl = round(rnd.uniform(-0.02, 0.08), 4)
    trades = rnd.randint(20, 120)
    return {
        "sharpe": sharpe,
        "winrate": winrate,
        "pnl": pnl,
        "trades": trades,
        "lookback_h": lookback_hours,
    }


def simple_autoupdate(cfg, result):
    """
    非激进的自动调参示例：
      - 夏普 < 0：降低 max_pos
      - 夏普 > 1.2：微增 max_pos
      - 轻微调整止损/止盈避免过拟合
    """
    risk = dict(cfg["risk"])
    if result["sharpe"] < 0:
        risk["max_pos"] = max(0.05, round(risk.get("max_pos", 0.3) * 0.9, 3))
    elif result["sharpe"] > 1.2:
        risk["max_pos"] = min(0.8, round(risk.get("max_pos", 0.3) * 1.05, 3))

    # 轻微抖动
    risk["stop"] = max(0.005, round(risk.get("stop", 0.02) * 1.0, 3))
    risk["take"] = max(0.01,  round(risk.get("take", 0.04) * 1.0, 3))
    cfg["risk"] = risk
    return cfg


def save_cfg(cfg):
    os.makedirs("config", exist_ok=True)
    # 保持 keys 顺序，便于 diff
    with open("config/params.json", "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2, sort_keys=False)
        f.write("\n")


def main():
    start = time.time()
    cfg = load_cfg()

    print(f"[{datetime.utcnow().isoformat()}Z] Start training with cfg:")
    print(json.dumps(cfg, ensure_ascii=False, indent=2))

    # 回测
    result = walk_forward_backtest(cfg["symbols"], cfg["interval"], cfg["risk"], 24, cfg.get("logic"))
    print("Backtest result:", json.dumps(result, ensure_ascii=False))

    # 自动微调并保存
    new_cfg = simple_autoupdate(cfg, result)
    save_cfg(new_cfg)

    print("New cfg saved to config/params.json:")
    print(json.dumps(new_cfg, ensure_ascii=False, indent=2))

    took = round(time.time() - start, 2)
    print(f"Training finished in {took}s")


if __name__ == "__main__":
    main()