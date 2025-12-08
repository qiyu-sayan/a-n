import os
import requests
import json

WECHAT_WEBHOOK = os.getenv("WECHAT_WEBHOOK", None)


def send_wecom_message(text: str) -> None:
    """
    发送纯文本消息到企业微信机器人
    """
    if not WECHAT_WEBHOOK:
        print("[WeCom] 未设置 WECHAT_WEBHOOK，跳过发送。")
        return

    payload = {
        "msgtype": "text",
        "text": {
            "content": text
        }
    }

    try:
        r = requests.post(WECHAT_WEBHOOK, json=payload)
        if r.status_code != 200:
            print(f"[WeCom] 发送失败：{r.text}")
    except Exception as e:
        print(f"[WeCom] 异常: {e}")


def send_wecom_markdown(text: str) -> None:
    """
    与 main.py 保持兼容的 markdown 通知。
    企业微信 markdown 要求 msgtype=markdown，但这里先用文本发，保持兼容。
    """
    send_wecom_message(text)
