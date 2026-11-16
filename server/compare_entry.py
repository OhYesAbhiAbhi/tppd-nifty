"""Replay a day with real Kite minute candles: what would the straddle (30% SL/leg, 15:15 exit) have made
if entered at a given time?   Usage: venv/bin/python compare_entry.py 09:20 [10:00 ...]   (read-only, places nothing)"""
import json, sys, datetime as dt
from kiteconnect import KiteConnect
from notify import charges

c = json.load(open("config.json"))
k = KiteConnect(api_key=c["api_key"]); k.set_access_token(open("access_token.txt").read().strip())
day = dt.date.today(); QTY = 65 * int(c.get("lots", 1)); SL = c.get("sl_pct", 0.30)
inst = [i for i in k.instruments("NFO") if i["name"] == "NIFTY" and i["segment"] == "NFO-OPT"]
exps = sorted({i["expiry"] for i in inst if i["expiry"] >= day}); exp = exps[1] if exps[0] == day else exps[0]
tab = {(i["instrument_type"], int(i["strike"])): i for i in inst if i["expiry"] == exp}
t0, t1 = dt.datetime.combine(day, dt.time(9, 15)), dt.datetime.combine(day, dt.time(15, 30))
spot = {x["date"].strftime("%H:%M"): x["open"] for x in k.historical_data(256265, t0, t1, "minute")}

def candles(tok):
    return [(x["date"].strftime("%H:%M"), x["open"], x["high"], x["low"], x["close"]) for x in k.historical_data(tok, t0, t1, "minute")]

for et in (sys.argv[1:] or ["09:20"]):
    s = spot.get(et) or list(spot.values())[0]; atm = int(round(s / 50) * 50)
    tot = 0; sv = bv = 0; lines = []
    for typ in ("CE", "PE"):
        cs = [x for x in candles(tab[(typ, atm)]["instrument_token"]) if x[0] >= et]
        entry = cs[0][1]; slp = round(entry * (1 + SL), 2); exit_px, why, when = None, "15:15 exit", None
        for hm, o, h, l, cl in cs:
            if hm >= "15:15": exit_px, when = o, hm; break
            if h >= slp: exit_px, why, when = max(slp, o), "stop-loss", hm; break
        if exit_px is None: exit_px, when = cs[-1][4], cs[-1][0]
        pnl = (entry - exit_px) * QTY; tot += pnl; sv += entry * QTY; bv += exit_px * QTY
        lines.append(f"  {atm}{typ}: sold {entry} -> {exit_px} ({why} {when})  Rs {pnl:,.0f}")
    ch = charges(sv, bv, 4)
    print(f"Entry {et}  Nifty {s:.1f}  ATM {atm}  expiry {exp}")
    print("\n".join(lines)); print(f"  gross Rs {tot:,.0f}  charges ~Rs {ch:,.0f}  net Rs {tot-ch:,.0f}\n")
print("Note: candle replay - fills assumed at candle open / stop price, so real fills may differ by a few points.")
