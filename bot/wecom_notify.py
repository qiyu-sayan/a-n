# bot/wecom_notify.py
import os, json, time, traceback

try:
    import requests
except Exception:
    requests = None  # 交给 workflow 安装 requests

def wecom_notify(text: str, at_all: bool = False):
    """
    企业微信文本消息推送。环境变量 WECHAT_WEBHOOK 必须预先配置。
    """
    url = os.getenv("WECHAT_WEBHOOK", "").strip()
    if not url or not requests:
        return
    payload = {
        "msgtype": "text",
        "text": {
            "content": text,
            "mentioned_list": ["@all"] if at_all else []
        }
    }
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception:
        pass


def wrap_run(run_callable):
    """
    统一包装：开跑通知 / 异常通知（简短堆栈）/ 结束通知（耗时）。
    不改变你的策略逻辑。
    """
    run_no = os.getenv("GITHUB_RUN_NUMBER", "local")
    start = time.time()
    wecom_notify(f"▶️ Run #{run_no} 开始")
    try:
        result = run_callable()
        dur = int(time.time() - start)
        wecom_notify(f"✅ Run #{run_no} 结束，用时 {dur}s")
        return result
    except Exception as e:
        tb = traceback.format_exc(limit=6)
        short_tb = "\n".join(tb.splitlines()[-12:])
        wecom_notify(f"❌ Run #{run_no} 异常：{e}\n{short_tb}")
        raise


def warn_451(url: str):
    """针对 451 地区限制的专用提醒。检测到就发一条，不抛异常。"""
    wecom_notify(f"⚠️ 收到 HTTP 451（地区限制）：\n{url}\n建议切换出海网络或改用兼容的行情源")