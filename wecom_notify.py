"""
wecom_notify.py

简单的企业微信机器人推送封装：
- 从环境变量 WECOM_WEBHOOK 读取 webhook 地址
- 提供 send_wecom_text(content: str) 供其他模块调用
"""

import os
import json
from typing import Any, Dict

import requests

WEBHOOK_URL = os.environ.get("WECOM_WEBHOOK", "")


def send_wecom_text(content: str) -> None:
    """
    发送一条 markdown 消息到企业微信群机器人。
    """
    if not WEBHOOK_URL:
        print("[WeCom] WECOM_WEBHOOK not set, skip push.")
        return

    payload: Dict[str, Any] = {
        "msgtype": "markdown",
        "markdown": {
            "content": content
        }
    }

    try:
        resp = requests.post(WEBHOOK_URL, json=payload, timeout=10)
    except Exception as e:
        print(f"[WeCom] request failed: {e}")
        return

    try:
        data = resp.json()
        print(f"[WeCom] resp: {data}")
    except Exception:
        print(f"[WeCom] status={resp.status_code}, text={resp.text}")


if __name__ == "__main__":
    # 手动测试用：在 Actions 里单独跑 python wecom_notify.py 看是否能收到消息
    send_wecom_text("测试消息：机器人部署成功。")
