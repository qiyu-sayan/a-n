import os
import json
import time
import traceback
from typing import Optional

import requests


def _post_wecom(webhook: str, payload: dict) -> None:
    """向企业微信机器人发送原始 payload。"""
    try:
        resp = requests.post(webhook, json=payload, timeout=5)
        resp.raise_for_status()
        data = resp.json()
        if data.get("errcode") != 0:
            print(f"[WECOM] send failed: {data}")
        else:
            print("[WECOM] send ok")
    except Exception as e:
        print(f"[WECOM] exception: {e}")
        traceback.print_exc()


def send_text(content: str, webhook: Optional[str] = None) -> None:
    """
    发送纯文本消息到企业微信机器人。

    - 默认从环境变量 WECOM_WEBHOOK 读取 webhook。
    - main.py / 其他模块只需要：from wecom_notify import send_text
    """
    if webhook is None:
        webhook = os.getenv("WECOM_WEBHOOK", "").strip()

    if not webhook:
        print("[WECOM] no webhook configured, message below:")
        print(content)
        return

    payload = {
        "msgtype": "text",
        "text": {
            "content": content,
        },
    }
    _post_wecom(webhook, payload)


def send_markdown(content: str, webhook: Optional[str] = None) -> None:
    """
    发送 markdown 消息（如果以后需要富文本可以用这个）。
    """
    if webhook is None:
        webhook = os.getenv("WECOM_WEBHOOK", "").strip()

    if not webhook:
        print("[WECOM] no webhook configured, markdown below:")
        print(content)
        return

    payload = {
        "msgtype": "markdown",
        "markdown": {
            "content": content,
        },
    }
    _post_wecom(webhook, payload)


if __name__ == "__main__":
    # 方便用 ping-wecom.yml 做健康检查：python wecom_notify.py "hello"
    import sys

    msg = "测试消息：" + time.strftime("%Y-%m-%d %H:%M:%S")
    if len(sys.argv) > 1:
        msg = " ".join(sys.argv[1:])

    print(f"[WECOM] sending test: {msg}")
    send_text(msg)
