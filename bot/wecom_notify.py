# bot/wecom_notify.py
"""
企业微信通知模块

优先使用「企业微信应用」的方式推送：
- 环境变量（支持多种写法，取第一个有值的）：
    WECOM_CORP_ID / WECOM_CORPID
    WECOM_AGENT_ID / WECOM_AGENTID
    WECOM_CORP_SECRET / WECOM_SECRET
    WECOM_TOUSER            (接收人，默认为 "@all")

如果你用的是 webhook 机器人，也可以设置：
    WECOM_WEBHOOK_KEY

两者都存在时，优先企业应用，其次 webhook。
"""

from __future__ import annotations

import json
import os
import time
from typing import Optional

import requests

# 关闭 https 的烦人告警（GitHub Actions 环境下偶尔会有）
requests.packages.urllib3.disable_warnings()  # type: ignore


def _get_env(*names: str) -> Optional[str]:
    """按顺序尝试多个环境变量名字，返回第一个非空值。"""
    for name in names:
        val = os.getenv(name)
        if val:
            return val
    return None


# ============ 企业微信应用方式 ============

def _get_wecom_app_config():
    corp_id = _get_env("WECOM_CORP_ID", "WECOM_CORPID")
    agent_id = _get_env("WECOM_AGENT_ID", "WECOM_AGENTID")
    corp_secret = _get_env("WECOM_CORP_SECRET", "WECOM_SECRET")
    to_user = os.getenv("WECOM_TOUSER", "@all")
    if corp_id and agent_id and corp_secret:
        return {
            "corp_id": corp_id,
            "agent_id": agent_id,
            "corp_secret": corp_secret,
            "to_user": to_user,
        }
    return None


def _get_wecom_token(cfg) -> Optional[str]:
    url = (
        "https://qyapi.weixin.qq.com/cgi-bin/gettoken"
        f"?corpid={cfg['corp_id']}&corpsecret={cfg['corp_secret']}"
    )
    try:
        resp = requests.get(url, timeout=5)
        data = resp.json()
        if data.get("errcode") == 0:
            return data.get("access_token")
        print(f"[wecom] 获取 access_token 失败: {data}")
    except Exception as e:
        print(f"[wecom] 请求 access_token 出错: {e}")
    return None


def _send_wecom_app_text(text: str) -> bool:
    cfg = _get_wecom_app_config()
    if not cfg:
        return False

    token = _get_wecom_token(cfg)
    if not token:
        return False

    url = f"https://qyapi.weixin.qq.com/cgi-bin/message/send?access_token={token}"
    payload = {
        "touser": cfg["to_user"],
        "msgtype": "text",
        "agentid": int(cfg["agent_id"]),
        "text": {"content": text},
        "safe": 0,
    }
    try:
        resp = requests.post(url, json=payload, timeout=5)
        data = resp.json()
        if data.get("errcode") == 0:
            return True
        print(f"[wecom] 发送应用消息失败: {data}")
    except Exception as e:
        print(f"[wecom] 发送应用消息异常: {e}")
    return False


# ============ webhook 机器人方式（可选） ============

def _get_wecom_webhook_url() -> Optional[str]:
    """
    兼容多种写法：
    - WECOM_WEBHOOK_KEY : 只填 key
    - WECOM_WEBHOOK     : 可以填 key，也可以直接填完整 URL
    - WECHAT_WEBHOOK    : 同上
    """
    raw = (
        os.getenv("WECOM_WEBHOOK_KEY")
        or os.getenv("WECOM_WEBHOOK")
        or os.getenv("WECHAT_WEBHOOK")
    )
    if not raw:
        return None

    raw = raw.strip()
    # 如果已经是完整 URL，直接用
    if raw.startswith("http://") or raw.startswith("https://"):
        return raw

    # 否则当作 key 拼接
    return f"https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key={raw}"



def _send_wecom_webhook(text: str) -> bool:
    url = _get_wecom_webhook_url()
    if not url:
        return False
    payload = {"msgtype": "text", "text": {"content": text}}
    try:
        resp = requests.post(url, json=payload, timeout=5)
        data = resp.json()
        if data.get("errcode") == 0:
            return True
        print(f"[wecom] webhook 发送失败: {data}")
    except Exception as e:
        print(f"[wecom] webhook 发送异常: {e}")
    return False


# ============ 对外暴露的统一函数 ============

def send_wecom_message(text: str) -> None:
    """
    对外统一调用的发送函数：
    1. 优先尝试企业微信应用
    2. 如果没有配置，就尝试 webhook
    """
    text = text.strip()
    if not text:
        return

    if _send_wecom_app_text(text):
        return

    if _send_wecom_webhook(text):
        return

    print("[wecom] 未配置有效的企业微信推送方式，消息内容如下：")
    print(text)
