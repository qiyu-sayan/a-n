# .github/wecom_notify.py
import sys, json, urllib.request

def main():
    if len(sys.argv) < 3:
        print("usage: wecom_notify.py <webhook> <message>")
        sys.exit(0)
    hook = sys.argv[1]
    msg  = sys.argv[2]
    payload = {"msgtype": "text", "text": {"content": msg[:1900]}}
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req  = urllib.request.Request(hook, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            print("WeCom:", r.status)
    except Exception as e:
        print("WeCom error:", e)

if __name__ == "__main__":
    main()