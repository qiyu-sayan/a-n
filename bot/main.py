import os, time, argparse, yaml, requests, pandas as pd, datetime as dt
from dateutil.tz import tzlocal

def now():
    return dt.datetime.now(tzlocal())

def load_cfg(p):
    with open(p, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)

def ensure_dirs(cfg):
    os.makedirs(cfg.get("data_dir","data"), exist_ok=True)
    os.makedirs(os.path.dirname(cfg.get("log_file","logs/app.log")), exist_ok=True)

def interval(cfg):
    return cfg.get("fast_interval",5) if cfg.get("mode","normal")=="fast" else cfg.get("normal_interval",60)

def fetch_price(symbol="ethereum", currency="usd", timeout=10):
    url="https://api.coingecko.com/api/v3/simple/price"
    q={"ids":symbol,"vs_currencies":currency}
    r=requests.get(url, params=q, timeout=timeout)
    r.raise_for_status()
    return float(r.json()[symbol][currency])

def append_csv(path, row):
    df=pd.DataFrame([row])
    hdr=not os.path.exists(path)
    df.to_csv(path, mode="a", header=hdr, index=False, encoding="utf-8")

def compute_signal(csv_path):
    try:
        df=pd.read_csv(csv_path)
        if len(df)<60: return "WAIT"
        s=pd.Series(df["price"].values)
        sma20=s.rolling(20).mean().iloc[-1]
        sma50=s.rolling(50).mean().iloc[-1]
        if pd.isna(sma20) or pd.isna(sma50): return "WAIT"
        if sma20>sma50: return "LONG"
        if sma20<sma50: return "FLAT"
        return "WAIT"
    except Exception:
        return "WAIT"

def log_line(path, text):
    with open(path, "a", encoding="utf-8") as f:
        f.write(text+"\n")

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--config", default="config.yaml")
    args=ap.parse_args()
    cfg=load_cfg(args.config)
    ensure_dirs(cfg)
    data_csv=os.path.join(cfg["data_dir"],"prices.csv")
    logf=cfg["log_file"]
    sym=cfg.get("symbol","ethereum")
    cur=cfg.get("currency","usd")
    while True:
        t=now()
        try:
            price=fetch_price(sym, cur, timeout=15)
            row={"timestamp":t.isoformat(), "price":price}
            append_csv(data_csv, row)
            sig=compute_signal(data_csv)
            line=f"{t.isoformat()} price={price} signal={sig} mode={cfg.get('mode')}"
            print(line, flush=True)
            log_line(logf, line)
        except Exception as e:
            line=f"{t.isoformat()} error={str(e)}"
            print(line, flush=True); log_line(logf, line)
        time.sleep(interval(cfg))

if __name__=="__main__":
    main()