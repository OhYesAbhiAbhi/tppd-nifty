"""
Market data collector (read-only, uses Kite historical API with today's login).

  venv/bin/python collector.py daily            # today's 1-minute candles: Nifty, India VIX, and Nifty options
                                                #   (2 nearest weekly expiries, strikes +/-600 around the day's open, with OI)
  venv/bin/python collector.py backfill 2024-10-01   # 1-minute Nifty + VIX history from that date to today (chunked)

Output (gzip CSV) under ~/algo/data/:
  data/daily/YYYYMMDD/nifty.csv.gz, vix.csv.gz, options.csv.gz
  data/hist/nifty_1m.csv.gz, vix_1m.csv.gz
Option history disappears from Zerodha once a contract expires, so `daily` runs every weekday at 15:40.
"""
import json, os, sys, csv, gzip, time, datetime as dt
from kiteconnect import KiteConnect

os.chdir(os.path.dirname(os.path.abspath(__file__)))
NIFTY, VIX = 256265, 264969
c = json.load(open("config.json"))
k = KiteConnect(api_key=c["api_key"]); k.set_access_token(open("access_token.txt").read().strip())

def hist(tok, a, b, oi=False):
    for i in range(4):
        try:
            time.sleep(0.36)                              # historical API ~3 requests/second
            return k.historical_data(tok, a, b, "minute", oi=oi)
        except Exception as e:
            if i == 3: print(f"  failed {tok}: {e}"); return []
            time.sleep(2 * (i + 1))

def write(path, rows, cols):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with gzip.open(path, "wt", newline="") as f:
        w = csv.writer(f); w.writerow(cols); w.writerows(rows)
    print(f"  {path}: {len(rows):,} rows")

def daily(day=None):
    day = day or dt.date.today()
    a, b = dt.datetime.combine(day, dt.time(9, 15)), dt.datetime.combine(day, dt.time(15, 30))
    out = f"data/daily/{day:%Y%m%d}"
    n = hist(NIFTY, a, b)
    if not n: print("No Nifty candles (holiday or not logged in?)"); return
    write(f"{out}/nifty.csv.gz", [[x["date"].strftime("%H:%M"), x["open"], x["high"], x["low"], x["close"]] for x in n], ["t", "o", "h", "l", "c"])
    v = hist(VIX, a, b)
    write(f"{out}/vix.csv.gz", [[x["date"].strftime("%H:%M"), x["open"], x["high"], x["low"], x["close"]] for x in v], ["t", "o", "h", "l", "c"])
    open_ = n[0]["open"]; lo, hi = round(open_ / 50) * 50 - 600, round(open_ / 50) * 50 + 600
    inst = [i for i in k.instruments("NFO") if i["name"] == "NIFTY" and i["segment"] == "NFO-OPT" and i["expiry"] >= day]
    exps = sorted({i["expiry"] for i in inst})[:2]
    pick = [i for i in inst if i["expiry"] in exps and lo <= i["strike"] <= hi]
    rows = []
    for i in sorted(pick, key=lambda i: (i["expiry"], i["strike"], i["instrument_type"])):
        for x in hist(i["instrument_token"], a, b, oi=True):
            rows.append([x["date"].strftime("%H:%M"), str(i["expiry"]), int(i["strike"]), i["instrument_type"],
                         x["open"], x["high"], x["low"], x["close"], x.get("volume", 0), x.get("oi", 0)])
    write(f"{out}/options.csv.gz", rows, ["t", "expiry", "strike", "type", "o", "h", "l", "c", "volume", "oi"])

def backfill(start):
    s = dt.datetime.strptime(start, "%Y-%m-%d"); end = dt.datetime.now()
    for tok, name in ((NIFTY, "nifty"), (VIX, "vix")):
        rows, a = [], s
        while a < end:
            b = min(a + dt.timedelta(days=59), end)
            got = hist(tok, a, b)
            rows += [[x["date"].strftime("%Y-%m-%d %H:%M"), x["open"], x["high"], x["low"], x["close"]] for x in got]
            print(f"  {name} {a:%Y-%m-%d}..{b:%Y-%m-%d}: {len(got):,}")
            a = b + dt.timedelta(days=1)
        write(f"data/hist/{name}_1m.csv.gz", rows, ["ts", "o", "h", "l", "c"])

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "backfill": backfill(sys.argv[2] if len(sys.argv) > 2 else "2024-10-01")
    else: daily()
