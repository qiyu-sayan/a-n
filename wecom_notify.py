import os
import json
import requests
from datetime import datetime
from typing import Optional, Dict


WECOM_WEBHOOK = os.getenv("WECOM_WEBHOOK")


def _send_wecom(payload: dict):
    if not WECOM_WEBHOOK:
        print("[WECOM MOCK]", payload)
        return

    try:
        r = requests.post(WECOM_WEBHOOK, json=payload, timeout=5)
        if r.status_code != 200:
            print(f"[WECOM ERROR] status={r.status_code}, body={r.text}")
    except Exception as e:
        print(f"[WECOM ERROR] {e}")


# ------------------------------------------------------------------
# 基础发送接口（兼容旧调用）
# ------------------------------------------------------------------
def send_text(text: str):
    payload = {
        "msgtype": "text",
        "text": {
            "content": text
        }
    }
    _send_wecom(payload)


def send_markdown(md: str):
    payload = {
        "msgtype": "markdown",
        "markdown": {
            "content": md
        }
    }
    _send_wecom(payload)


# ------------------------------------------------------------------
# 语义化通知接口（推荐使用）
# ------------------------------------------------------------------
def notify_open(
    symbol: str,
    side: str,
    price: float,
    size: float,
    leverage: int,
    signal_info: Optional[Dict] = None
):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    md = f"""### 🚀 开仓
- 时间：{ts}
- 标的：**{symbol}**
- 方向：**{side}**
- 价格：{price}
- 数量：{size}
- 杠杆：{leverage}x
"""

    if signal_info:
        md += "\n**信号摘要：**\n"
        for k, v in signal_info.items():
            md += f"- {k}: {v}\n"

    send_markdown(md)


def notify_close(
    symbol: str,
    side: str,
    entry_price: float,
    exit_price: float,
    pnl_usdt: float,
    pnl_pct: float,
    reason: str
):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    emoji = {
        "TP": "🎯",
        "SL": "🛑",
        "MANUAL": "✋",
        "BOT": "🤖"
    }.get(reason, "📦")

    md = f"""### {emoji} 平仓
- 时间：{ts}
- 标的：**{symbol}**
- 方向：**{side}**
- 开仓价：{entry_price}
- 平仓价：{exit_price}
- 盈亏：**{pnl_usdt:.2f} USDT ({pnl_pct:.2f}%)**
- 原因：**{reason}**
"""
    send_markdown(md)


def notify_error(title: str, detail: str):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    md = f"""### ❗ 异常告警
- 时间：{ts}
- 类型：**{title}**

