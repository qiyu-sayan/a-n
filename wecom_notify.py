import os
import requests
from datetime import datetime
from typing import Optional, Dict, Any


def _get_webhook(webhook: Optional[str] = None) -> str:
    if webhook:
        return webhook.strip()
    return os.getenv("WECOM_WEBHOOK", "").strip()


def _post(payload: Dict[str, Any], webhook: Optional[str] = None) -> None:
    url = _get_webhook(webhook)
    if not url:
        print("[WECOM MOCK]", payload)
        return

    try:
        r = requests.post(url, json=payload, timeout=8)
        r.raise_for_status()
        data = r.json()
        if data.get("errcode") != 0:
            print("[WECOM ERROR]", data)
    except Exception as e:
        print("[WECOM ERROR]", repr(e))


def send_text(content: str, webhook: Optional[str] = None) -> None:
    _post(
        {
            "msgtype": "text",
            "text": {"content": content},
        },
        webhook=webhook,
    )


def send_markdown(content: str, webhook: Optional[str] = None) -> None:
    _post(
        {
            "msgtype": "markdown",
            "markdown": {"content": content},
        },
        webhook=webhook,
    )


def notify_error(title: str, detail: str, webhook: Optional[str] = None) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    # 不用三引号，避免“未闭合”这种低级事故
    md = (
        "### ❗ 异常告警\n"
        f"- 时间：{ts}\n"
        f"- 类型：**{title}**\n\n"
        "```\n"
        f"{detail}\n"
        "```\n"
    )
    send_markdown(md, webhook=webhook)


def notify_open(symbol: str, side: str, price: float, size: float, leverage: int, signal_info: Optional[Dict] = None,
                webhook: Optional[str] = None) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    md = (
        "### 🚀 开仓\n"
        f"- 时间：{ts}\n"
        f"- 标的：**{symbol}**\n"
        f"- 方向：**{side}**\n"
        f"- 价格：{price}\n"
        f"- 数量：{size}\n"
        f"- 杠杆：{leverage}x\n"
    )
    if signal_info:
        md += "\n**信号摘要：**\n"
        for k, v in signal_info.items():
            md += f"- {k}: {v}\n"
    send_markdown(md, webhook=webhook)


def notify_close(symbol: str, side: str, entry_price: float, exit_price: float, pnl_usdt: float, pnl_pct: float,
                 reason: str, webhook: Optional[str] = None) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    emoji = {"TP": "🎯", "SL": "🛑", "MANUAL": "✋", "BOT": "🤖"}.get(reason, "📦")

    md = (
        f"### {emoji} 平仓\n"
        f"- 时间：{ts}\n"
        f"- 标的：**{symbol}**\n"
        f"- 方向：**{side}**\n"
        f"- 开仓价：{entry_price}\n"
        f"- 平仓价：{exit_price}\n"
        f"- 盈亏：**{pnl_usdt:.2f} USDT ({pnl_pct:.2f}%)**\n"
        f"- 原因：**{reason}**\n"
    )
    send_markdown(md, webhook=webhook)
