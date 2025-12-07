# bot/wecom_notify.py
"""
企业微信机器人通知模块

使用方式：
    from bot.wecom_notify import send_wecom_message
    send_wecom_message("hello world")

需要环境变量/Secrets:
    WECOM_WEBHOOK  = 企业微信机器人 webhook URL
"""

import os
import json
import requests


def _get_webhook() -> str:
    webhook = os.getenv("WECOM_WEBHOOK", "").strip()
    if not webhook:
        raise RuntimeError("WECOM_WEBHOOK 未配置，无法发送企业微信消息")
    return webhook


def send_wecom_message(text: str) -> None:
    """
    发送一条 markdown 消息到企业微信机器人
    """
    webhook = _get_webhook()

    payload = {
        "msgtype": "markdown",
        "markdown": {
            "content": text,
        },
    }

    try:
        resp = requests.post(webhook, data=json.dumps(payload), timeout=5)
        resp.raise_for_status()
    except Exception as e:
        # 不抛异常，避免影响主流程；只在日志里打印
        print(f"[WeCom] 发送失败: {e}")
