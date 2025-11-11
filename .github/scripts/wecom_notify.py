#!/usr/bin/env python3
# coding: utf-8
"""
WeCom (企业微信) webhook 简单推送脚本
用法：
  python .github/scripts/wecom_notify.py "<webhook_url>" "<text_message>"
或从环境变量读：
  WECHAT_WEBHOOK, MSG
"""

import json
import os
import sys
import urllib.request

def main():
    hook = None
    msg = None

    if len(sys.argv) >= 2:
        hook = sys.argv[1]
    if len(sys.argv) >= 3:
        msg = sys.argv[2]

    # 回退到环境变量
    hook = hook or os.environ.get("WECHAT_WEBHOOK") or ""
    msg = msg or os.environ.get("MSG") or ""

    if not hook.strip():
        print("Skip WeCom notify: empty webhook")
        return

    if not msg:
        msg = "(empty message)"

    payload = {"msgtype": "text", "text": {"content": msg}}
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        hook,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            body = r.read().decode("utf-8", errors="ignore")
            print("WeCom OK:", body[:200])
    except Exception as e:
        print("WeCom error:", e)
        # 不导致整个 Job 失败
        # 如要失败请改为：sys.exit(1)

if __name__ == "__main__":
    main()