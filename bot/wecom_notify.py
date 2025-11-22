# bot/wecom_notify.py
import os
import requests

# 从环境变量里拿企业微信机器人 Webhook（GitHub Actions 那边已经通过 env 传进来了）
WEBHOOK = os.environ.get("WECHAT_WEBHOOK", "")


def wecom_notify(text: str) -> None:
    """
    发送一条企业微信 Markdown 消息。
    text: 要发送的文本内容（支持换行）
    """
    if not WEBHOOK:
        print("[wecom] WECHAT_WEBHOOK 未配置，跳过发送")
        return

    payload = {
        "msgtype": "markdown",
        "markdown": {
            "content": text
        },
    }

    try:
        resp = requests.post(WEBHOOK, json=payload, timeout=5)
        print(f"[wecom] status={resp.status_code}, resp={resp.text[:200]}")
    except Exception as e:
        print(f"[wecom] 发送失败: {e}")
