"""Shadow strategies: rule variations tracked on the same live prices, never traded.
Each day's result per variant goes to shadow.csv so we can compare them over weeks."""
import datetime as dt
from market import last, bid, ask
from notify import charges

NIFTY = "NSE:NIFTY 50"
VARIANTS = [
    dict(name="S30 (live rules)", off=0,   sl=0.30),
    dict(name="S25",              off=0,   sl=0.25),
    dict(name="S40",              off=0,   sl=0.40),
    dict(name="S30 + breakeven",  off=0,   sl=0.30, be=True),     # after one leg stops, other leg's stop -> its entry
    dict(name="S30 + profit lock",off=0,   sl=0.30, lock=True),   # once profit >= 30% of credit, exit if it halves
    dict(name="Strangle150 SL50", off=150, sl=0.50),
    dict(name="S30 skip VIX>+5%", off=0,   sl=0.30, vixskip=True),
    # --- indicator ideas (added 22 Oct): "exit when there's no chance of coming back" ---
    dict(name="S30 + trend exit", off=0, sl=0.30, trend="always"),      # exit a leg when 15m Supertrend(10,3) turns against it
    dict(name="S30 + trend exit after 1st stop", off=0, sl=0.30, trend="after_stop"),  # same, only for the surviving leg
    dict(name="S30 + tight SL after 1st stop", off=0, sl=0.30, tight=0.15),  # surviving leg's stop -> entry +15%
    # --- profit-protection ideas (added 22 Oct evening): "could we have exited at the 11:15 peak?" ---
    dict(name="S30 + green lock after 1st stop", off=0, sl=0.30, green_lock=500),  # after a stop, once day is >= +Rs500, exit if it falls back to Rs0
    dict(name="S30 + small profit lock", off=0, sl=0.30, lock_small=0.05),         # arm at +5% of credit, exit if profit halves from peak
    # added 25 Oct: day filter from the 2020-2026 study (claude/day-filter-study.md). Expiry day always traded.
    dict(name="S30 day filter", off=0, sl=0.30, dayfilter="all"),          # sit out a non-expiry day if any warning flag is on
    dict(name="S30 expiry + clean Monday", off=0, sl=0.30, dayfilter="mon"),  # trade only expiry day and flag-free DTE-1 days
    # added 25 Oct: later entries (entry-time study). These enter on their own, after 9:20, at the ATM strike of that moment.
    dict(name="Wed-Fri wait for calm", off=0, sl=0.30, dte=(4, 5, 6), noflags=True, late=("09:45", "10:15", "10:45", "11:30"), calm=True),
    dict(name="Wed-Fri 10:45", off=0, sl=0.30, dte=(4, 5, 6), late=("10:45",)),
    dict(name="Wed-Fri 10:45 if VIX <= open", off=0, sl=0.30, dte=(4, 5, 6), late=("10:45",), calm=True),
    dict(name="Monday 11:30", off=0, sl=0.30, dte=(1,), late=("11:30",)),
]
VIX = "NSE:INDIA VIX"

# ---------------------------------------------------------------- 15-minute Nifty bars + Supertrend
def _bucket(t):
    m = t.hour * 60 + t.minute - (9 * 60 + 15)
    return t.date().isoformat() + "-" + str(m // 15)

def supertrend_dir(bars, n=10, mult=3.0):
    """bars: list of [o,h,l,c]. Returns +1 (up) / -1 (down) for the last bar, or 0 if not enough bars."""
    if len(bars) < n + 2: return 0
    trs = []
    for i, (o, h, l, c) in enumerate(bars):
        pc = bars[i - 1][3] if i else c
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    atr = sum(trs[:n]) / n; ub = lb = None; d = 1
    for i in range(len(bars)):
        if i >= n: atr = (atr * (n - 1) + trs[i]) / n
        o, h, l, c = bars[i]; mid = (h + l) / 2
        bu, bl = mid + mult * atr, mid - mult * atr
        pc = bars[i - 1][3] if i else c
        ub = bu if ub is None or bu < ub or pc > ub else ub
        lb = bl if lb is None or bl > lb or pc < lb else lb
        if i:
            if c > prev_ub: d = 1
            elif c < prev_lb: d = -1
        prev_ub, prev_lb = ub, lb
    return d

def _feed_bar(st, q):
    if NIFTY not in q: return False
    p = last(q, NIFTY); b = _bucket(dt.datetime.now())
    if st.get("bar_key") != b:
        closed = st.get("bar_key") is not None
        if closed: st["bars"].append(st["bar"])
        st["bar_key"], st["bar"] = b, [p, p, p, p]
        st["bars"] = st["bars"][-200:]
        if closed: st["trend"] = supertrend_dir(st["bars"])
        return closed
    o, h, l, c = st["bar"]; st["bar"] = [o, max(h, p), min(l, p), p]
    return False

# ---------------------------------------------------------------- lifecycle
def day_flags(q, lookup, atm, spot, day):
    """Warning flags for the day filter. day = dict(expiry_day, dte_week, vix_prev, vix_prev2) from the bot.
    dte_week = days to the current-week expiry (the contract the study was built on)."""
    f = []
    vp, vp2 = day.get("vix_prev"), day.get("vix_prev2")
    if vp and vp2 and (vp / vp2 - 1) * 100 > 3.32: f.append(f"VIX rose {(vp / vp2 - 1) * 100:.1f}% yesterday")
    if vp and vp <= 12.88: f.append(f"VIX {vp} <= 12.9")
    try:
        lk = day.get("week_lookup") or lookup
        strad = bid(q, "NFO:" + lk("CE", atm)) + bid(q, "NFO:" + lk("PE", atm))
        imp = strad / spot * 100 / (day["dte_week"] + 1) ** 0.5
        day["implied_daily"] = round(imp, 3)
        if imp <= 0.47: f.append(f"premium per day {imp:.2f}% <= 0.47%")
    except Exception as e:
        day["implied_daily"] = None
    return f

def _skip_reason(v, vix_chg_pct, day):
    if v.get("vixskip") and vix_chg_pct is not None and vix_chg_pct > 5: return "skipped (VIX up >5% at entry)"
    if v.get("dte"):
        if not day or day.get("expiry_day") or day.get("dte_week") not in v["dte"]:
            return f"skipped (only DTE {'/'.join(map(str, v['dte']))}; today DTE {day.get('dte_week') if day else '?'})"
        if v.get("noflags") and day.get("flags"): return "skipped (" + "; ".join(day["flags"]) + ")"
        return None
    mode = v.get("dayfilter")
    if not mode or not day or day.get("expiry_day"): return None
    if mode == "mon" and day.get("dte_week") != 1: return f"skipped (not expiry day or DTE 1; DTE {day.get('dte_week')})"
    if day.get("flags"): return "skipped (" + "; ".join(day["flags"]) + ")"
    return None

def start(q, lookup, atm, qty, vix_chg_pct, hist_bars=None, day=None):
    st = {"qty": qty, "variants": [], "bars": list(hist_bars or []), "bar": None, "bar_key": None, "day": day, "want": [], "chain": {}}
    for k in range(atm - 1500, atm + 1501, 50):          # symbols for late entries (JSON-safe, survives a restart)
        for typ in ("CE", "PE"):
            try: st["chain"][f"{typ}{k}"] = lookup(typ, k)
            except Exception: pass
    st["trend"] = supertrend_dir(st["bars"])
    for v in VARIANTS:
        why = _skip_reason(v, vix_chg_pct, day)
        d = {"name": v["name"], "p": v, "skipped": bool(why), "skip_why": why,
             "legs": [], "peak": 0.0, "mtm": 0.0, "done": False}
        if not d["skipped"] and v.get("late"):
            d["pending"] = True                              # enters later, in update()
        elif not d["skipped"]:
            for typ, k in (("CE", atm + v["off"]), ("PE", atm - v["off"])):
                key = "NFO:" + lookup(typ, k)
                e = bid(q, key)
                d["legs"].append(dict(key=key, typ=typ, entry=e, sl=round(e * (1 + v["sl"]), 2), closed=False, exit=None, why=None))
        else:
            d["done"] = True
        st["variants"].append(d)
    return st

def keys(st):
    return sorted({l["key"] for d in st["variants"] for l in d["legs"]} | set(st.get("want", [])))

def _late_entry(st, d, q, now_hm):
    """Pending late-entry variant: at each check time (and VIX calm, if required) enter the ATM straddle at the bid."""
    p = d["p"]
    if d.get("due"):                                       # strike chosen at the check time; enter once quotes arrive
        ks = d["due"]
        if all(k in q for k in ks):
            for typ, key in zip(("CE", "PE"), ks):
                e = bid(q, key); d["legs"].append(dict(key=key, typ=typ, entry=e, sl=round(e * (1 + p["sl"]), 2), closed=False, exit=None, why=None))
            d["pending"] = False; d["entered_at"] = now_hm; d.pop("due")
        return
    checks = [t for t in p["late"] if t <= now_hm and t not in d.setdefault("checked", [])]
    if not checks: return
    d["checked"] += checks
    if NIFTY not in q: return
    if p.get("calm"):
        try:
            v, vo = last(q, VIX), q[VIX]["ohlc"]["open"]
            if v > vo:
                if d["checked"] == list(p["late"]): d.update(done=True, skipped=True, skip_why=f"skipped (VIX above its open at every check; last {v} vs {vo})")
                return
        except Exception: return
    atm = int(round(last(q, NIFTY) / 50) * 50)
    try: ks = ["NFO:" + st["chain"][f"CE{atm}"], "NFO:" + st["chain"][f"PE{atm}"]]
    except KeyError: return
    d["due"] = ks; st["want"] = sorted(set(st.get("want", [])) | set(ks))
    if all(k in q for k in ks): _late_entry(st, d, q, now_hm)   # quotes already here -> enter now

def _close(l, px, why):
    l.update(closed=True, exit=round(px, 2), why=why)

def update(st, q, cap_rs, final=False):
    qty = st["qty"]
    st.setdefault("bars", []); st.setdefault("bar_key", None)
    bar_closed = _feed_bar(st, q)
    trend = st.get("trend", 0)
    now_hm = dt.datetime.now().strftime("%H:%M")
    for d in st["variants"]:
        if d["done"]: continue
        p = d["p"]
        if d.get("pending"):
            if final: d.update(done=True, skipped=True, skip_why=d.get("skip_why") or "skipped (entry condition never met)"); continue
            _late_entry(st, d, q, now_hm)
            if d.get("pending"): continue
        for l in d["legs"]:
            if l["closed"] or l["key"] not in q: continue
            lp, a = last(q, l["key"]), ask(q, l["key"])
            if lp >= l["sl"] or a >= l["sl"]:
                _close(l, max(l["sl"], a), "stop-loss")
                for o in d["legs"]:
                    if o["closed"]: continue
                    if p.get("be"): o["sl"] = min(o["sl"], o["entry"])
                    if p.get("tight"): o["sl"] = min(o["sl"], round(o["entry"] * (1 + p["tight"]), 2))
        # indicator exit, evaluated only when a 15-minute bar closes
        if p.get("trend") and bar_closed and trend != 0:
            one_stopped = any(l["closed"] and l["why"] == "stop-loss" for l in d["legs"])
            if p["trend"] == "always" or one_stopped:
                for l in d["legs"]:
                    if l["closed"] or l["key"] not in q: continue
                    typ = l.get("typ") or l["key"][-2:]
                    if (typ == "PE" and trend == -1) or (typ == "CE" and trend == 1):
                        _close(l, ask(q, l["key"]), "trend exit")
        mtm = sum((l["entry"] - (l["exit"] if l["closed"] else last(q, l["key"]))) * qty for l in d["legs"] if l["closed"] or l["key"] in q)
        d["mtm"] = round(mtm); d["peak"] = max(d["peak"], mtm)
        credit_rs = sum(l["entry"] for l in d["legs"]) * qty
        if p.get("green_lock") and mtm >= p["green_lock"] and any(l["closed"] and l["why"] == "stop-loss" for l in d["legs"]):
            d["armed"] = True
        why = None
        if mtm <= -abs(cap_rs): why = "loss cap"
        elif p.get("lock") and d["peak"] >= 0.3 * credit_rs and mtm <= 0.5 * d["peak"]: why = "profit lock"
        elif p.get("lock_small") and d["peak"] >= p["lock_small"] * credit_rs and mtm <= 0.5 * d["peak"]: why = "small profit lock"
        elif p.get("green_lock") and d.get("armed") and mtm <= 0: why = "green lock"
        elif final: why = "15:15 exit"
        if why:
            for l in d["legs"]:
                if not l["closed"] and l["key"] in q: _close(l, ask(q, l["key"]), why)
        if all(l["closed"] for l in d["legs"]): d["done"] = True

def results(st):
    qty, rows = st["qty"], []
    for d in st["variants"]:
        if d["skipped"]:
            rows.append(dict(variant=d["name"], gross=0, charges=0, net=0, detail=d.get("skip_why") or "skipped")); continue
        if not all(l["closed"] for l in d["legs"]):
            rows.append(dict(variant=d["name"], gross=None, charges=None, net=None, detail="incomplete")); continue
        gross = sum((l["entry"] - l["exit"]) * qty for l in d["legs"])
        sv = sum(l["entry"] for l in d["legs"]) * qty; bv = sum(l["exit"] for l in d["legs"]) * qty
        c = charges(sv, bv, 2 * len(d["legs"]))
        detail = (f"entered {d['entered_at']}: " if d.get("entered_at") else "") + "; ".join(f"{l['key'][4:]} {l['entry']}->{l['exit']} ({l['why']})" for l in d["legs"])
        rows.append(dict(variant=d["name"], gross=round(gross), charges=round(c), net=round(gross - c), detail=detail))
    return rows
