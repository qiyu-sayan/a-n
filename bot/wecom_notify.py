import os
import requests


# 优先使用 WECOM_WEBHOOK；兼容旧的 WECHAT_WEBHOOK
WECOM_WEBHOOK = (
    os.getenv("WECOM_WEBHOOK")
    or os.getenv("WECHAT_WEBHOOK")
)


def send_wecom_message(text: str) -> None:
    """
    通过企业微信机器人 webhook 发送纯文本消息
    """
    if not WECOM_WEBHOOK:
        print("[WeCom] 未设置 WECOM_WEBHOOK/WECHAT_WEBHOOK，跳过发送。")
        return

    payload = {
        "msgtype": "text",
        "text": {
            "content": text
        }
    }

    try:
        r = requests.post(WECOM_WEBHOOK, json=payload, timeout=5)
        if r.status_code != 200:
            print(f"[WeCom] 发送失败：{r.status_code} {r.text}")
    except Exception as e:
        print(f"[WeCom] 发送异常: {e}")


def send_wecom_markdown(text: str) -> None:
    """
    与 main.py 保持兼容的 markdown 接口。
    目前内部仍用纯文本发送，如果以后想用真正 markdown,
    可以在这里把 msgtype 改成 markdown。
    """
    send_wecom_message(text)
