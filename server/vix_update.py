"""Hourly Telegram update on India VIX and the measures the bot judges by (cron: 15 9-15 * * 1-5).
Read-only: one quote call per run, no orders."""
import json, os, glob, time, math, datetime as dt
import socket, urllib3.util.connection as _u3c
_u3c.allowed_gai_family = lambda: socket.AF_INET
from kiteconnect import KiteConnect
from notify import tg

os.chdir(os.path.dirname(os.path.abspath(__file__)))
c = json.load(open("config.json"))
G = dict(c, telegram_chat_id=c.get("telegram_group_chat_id") or c.get("telegram_chat_id"))
NOW = dt.datetime.now(); TODAY = NOW.date()

def years_left(expiry):
    """trading time to expiry 15:30 in years (375 trading minutes a day, 252 days a year)"""
    close = dt.datetime.combine(TODAY, dt.time(15, 30))
    today_min = max(0, (close - NOW).total_seconds() / 60)
    full = 0; d = TODAY + dt.timedelta(days=1)
    while d <= expiry:
        if d.weekday() < 5: full += 1
        d += dt.timedelta(days=1)
    return (today_min + full * 375) / (252 * 375)

def main():
    if TODAY.weekday() > 4: return
    k = KiteConnect(api_key=c["api_key"]); k.set_access_token(open("access_token.txt").read().strip())
    inst = [i for i in k.instruments("NFO") if i["name"] == "NIFTY" and i["segment"] == "NFO-OPT"]
    exps = sorted({i["expiry"] for i in inst if i["expiry"] >= TODAY})
    wk = exps[0]; expiry_day = wk == TODAY
    use = exps[1] if expiry_day else wk                       # the contract the bot sells today
    keys = ["NSE:INDIA VIX", "NSE:NIFTY 50"]
    q0 = None
    for i in range(3):
        try: q0 = k.quote(keys); break
        except Exception: time.sleep(1.5)
    if not q0: return
    spot = q0["NSE:NIFTY 50"]["last_price"]; atm = int(round(spot / 50) * 50)
    sym = {(i["instrument_type"], int(i["strike"]), i["expiry"]): i["tradingsymbol"] for i in inst if i["expiry"] in (wk, use)}
    okeys = [f"NFO:{sym[(t, atm, e)]}" for e in sorted({wk, use}) for t in ("CE", "PE") if (t, atm, e) in sym]
    time.sleep(1.2)
    q = None
    for i in range(3):
        try: q = k.quote(okeys); break
        except Exception: time.sleep(1.5)
    q = {**q0, **(q or {})}

    v = q["NSE:INDIA VIX"]; vix, vo, vh, vl, vpc = v["last_price"], v["ohlc"]["open"], v["ohlc"]["high"], v["ohlc"]["low"], v["ohlc"]["close"]
    n = q["NSE:NIFTY 50"]; npc, nh, nl = n["ohlc"]["close"], n["ohlc"]["high"], n["ohlc"]["low"]
    def strad(e):
        try: return q[f"NFO:{sym[('CE', atm, e)]}"]["last_price"] + q[f"NFO:{sym[('PE', atm, e)]}"]["last_price"]
        except Exception: return None
    lines = [f"VIX update {NOW:%a %d %b %H:%M}",
             f"India VIX {vix:.2f}  ({(vix / vpc - 1) * 100:+.1f}% vs yesterday {vpc:.2f})",
             f"  vs 9:15 open {vo:.2f}: {'CALM (at/below open)' if vix <= vo else 'RISING (above open)'}  | day range {vl:.2f}-{vh:.2f}",
             f"Nifty {spot:,.0f}  ({(spot / npc - 1) * 100:+.2f}%)  day range {nl:,.0f}-{nh:,.0f} ({(nh - nl) / spot * 100:.2f}%)"]
    s = strad(wk)
    if s:
        T = years_left(wk) if not expiry_day else None
        dte = (wk - TODAY).days
        pdl = s / spot * 100 / math.sqrt(dte + 1)
        lines.append(f"This week's ATM {atm} straddle ({wk:%d %b}, {dte}d): {s:.1f} pts = {s / spot * 100:.2f}% | premium per day {pdl:.2f}%"
                     + (" (cheap, <=0.47)" if pdl <= 0.47 else ""))
        if T and T > 0:
            lines.append(f"Weekly implied vol ~{s / (0.7979 * spot * math.sqrt(T)) * 100:.1f}%  (vs 30-day India VIX {vix:.1f})")
    # what the bot judges by
    wd = TODAY.weekday()
    rule = ("expiry day: sell 9:20, next week's contract" if expiry_day else
            {0: "Monday: sell 11:30 (no VIX check)", 1: "Tuesday (not expiry): sell 9:20"}.get(wd, "Wed-Fri: sell 10:45 only if VIX <= 9:15 open"))
    lines.append(f"Today's rule: {rule}")
    flags = []
    if (vix / vpc - 1) * 100 > 3.32: flags.append(f"VIX up {(vix / vpc - 1) * 100:.1f}% today (>3.3%)")
    if vix <= 12.88: flags.append("VIX <= 12.9 (premium too thin)")
    lines.append("Warning signs for tomorrow if this holds to the close: " + ("; ".join(flags) if flags else "none"))
    st = sorted(glob.glob(f"state_{TODAY:%Y%m%d}.json"))
    if st:
        s_ = json.load(open(st[-1])); legs = [l for l in s_.get("legs", []) if l.get("filled")]
        if legs:
            mtm = 0
            for l in legs:
                key = f"NFO:{l['symbol']}"
                px = l.get("exit") if l.get("closed") else None
                if px is None:
                    try: time.sleep(1.2); px = k.quote([key])[key]["last_price"]
                    except Exception: px = None
                if px is not None: mtm += (l["avg"] - px) * l["filled"]
            lines.append(f"Bot: {', '.join(l['symbol'][-7:] + ' ' + str(l['avg']) + ('->' + str(l['exit']) if l.get('closed') else ' open') for l in legs)}  MTM ~Rs {mtm:,.0f}")
        else:
            lines.append(f"Bot: no live position ({s_.get('exit_reason') or s_.get('phase')})")
    tg(G, "\n".join(lines))

if __name__ == "__main__":
    try: main()
    except Exception as e: tg(G, f"VIX update failed: {e}")
