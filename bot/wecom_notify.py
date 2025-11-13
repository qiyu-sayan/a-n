# bot/wecom_notify.py
"""
用法：
  env 里传入：
    WECHAT_WEBHOOK = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=..."
    MSG            = 任意文本（支持多行）
  然后运行：python -u bot/wecom_notify.py
"""
import os, json, sys, urllib.request

def wecom_notify():
    hook = os.getenv("WECHAT_WEBHOOK", "").strip()
    msg  = os.getenv("MSG", "").strip()
    if not hook:
        print("no WECHAT_WEBHOOK, skip")
        return
    payload = {"msgtype":"text", "text":{"content": msg[:19990]}}
    data = json.dumps(payload).encode("utf-8")
    try:
        req = urllib.request.Request(hook, data=data,
                headers={"Content-Type":"application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            print("wecom:", r.status)
    except Exception as e:
        print("wecom error:", e, file=sys.stderr)

if __name__ == "__main__":
    wecom_notify()