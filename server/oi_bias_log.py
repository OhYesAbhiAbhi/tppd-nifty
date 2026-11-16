#!/usr/bin/env python3
"""OI-bias overnight sell - SHADOW LOGGER (no orders are ever placed).

Rule being tracked (backtest: oi-bias-overnight-study.md):
  2-3 PM: call share = CE OI (ATM..ATM+250) / (that + PE OI ATM..ATM-250), nearest expiry AFTER today.
  share >= 0.60 -> sell CE (call writers heavy = bearish).  share <= 0.40 -> sell PE.
  Next morning 09:20: bias still there -> hold to expiry, else exit at 09:20.

What it records (oi_bias_journal.csv + oi_bias_state.json):
  snap    14:00 / 14:30 / 15:00 split, spot, and 15:00 LTPs of ATM/OTM2/OTM4 on both sides
  morning 09:20 spot, 09:20 OI split, option LTPs; decision by BOTH checks:
          idx  = Nifty at 09:20 vs 15:00 entry (what the backtest used)
          oi   = 09:20 OI split still on the same side of 50/50 (the real rule)
  settle  expiry-day close -> intrinsic value for positions still held

Usage (from ~/algo, with the bot's venv):
  venv/bin/python oi_bias_log.py snap      cron 13:58 weekdays (waits for 14:00, 14:30, 15:00)
  venv/bin/python oi_bias_log.py morning   cron 09:18 weekdays (waits for 09:20)
  venv/bin/python oi_bias_log.py settle    cron 15:32 weekdays
  venv/bin/python oi_bias_log.py now       one-off: print the split right now (no logging)
  venv/bin/python oi_bias_log.py report    summary of the journal
"""
import csv, json, os, sys, time
from datetime import timedelta

import extras as X   # reuse login, telegram, time helpers from extras.py (same folder)

BASE = X.BASE
JOURNAL = os.path.join(BASE, "oi_bias_journal.csv")
STATE = os.path.join(BASE, "oi_bias_state.json")
NAME = "oi_bias"
THR = 0.60
LOT_FALLBACK = 65
COLS = ["kind", "date", "time", "expiry", "spot", "atm", "split", "side",
        "otm", "strike", "symbol", "ltp", "note"]


def jlog(**r):
    new = not os.path.exists(JOURNAL)
    with open(JOURNAL, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLS)
        if new:
            w.writeheader()
        w.writerow({k: r.get(k, "") for k in COLS})


def load_state():
    return json.load(open(STATE)) if os.path.exists(STATE) else {"open": []}


def save_state(st):
    json.dump(st, open(STATE, "w"), indent=1, default=str)


def chain(kite, after_today=True):
    """Spot, expiry (first strictly after today), ATM and the option instruments by (strike, type)."""
    spot = kite.ltp(["NSE:NIFTY 50"])["NSE:NIFTY 50"]["last_price"]
    inst = [i for i in kite.instruments("NFO") if i.get("name") == "NIFTY" and i.get("instrument_type") in ("CE", "PE")]
    today = X.now().date()
    exps = sorted({i["expiry"] for i in inst if (i["expiry"] > today if after_today else i["expiry"] >= today)})
    exp = exps[0]
    by = {(int(i["strike"]), i["instrument_type"]): i for i in inst if i["expiry"] == exp}
    atm = int(round(spot / 50.0) * 50)
    return spot, exp, atm, by


def split_now(kite, spot, atm, by):
    ce = [by.get((atm + 50 * i, "CE")) for i in range(6)]
    pe = [by.get((atm - 50 * i, "PE")) for i in range(6)]
    syms = ["NFO:" + i["tradingsymbol"] for i in ce + pe if i]
    q = kite.quote(syms)
    C = sum(q["NFO:" + i["tradingsymbol"]].get("oi", 0) or 0 for i in ce if i)
    P = sum(q["NFO:" + i["tradingsymbol"]].get("oi", 0) or 0 for i in pe if i)
    return (C / (C + P) if C + P else None), C, P


def side_of(split):
    if split is None:
        return ""
    return "CE" if split >= THR else ("PE" if split <= 1 - THR else "")


def leg(by, atm, ty, otm):
    k = atm + 50 * otm if ty == "CE" else atm - 50 * otm
    return k, by.get((k, ty))


def cmd_snap():
    cfg = X.read_cfg()
    X.wait_until(X.at("13:59"))
    kite = X.login_retry(NAME, "15:00", "OI-bias shadow log")
    res = {}
    for hhmm in ("14:00", "14:30", "15:00"):
        X.wait_until(X.at(hhmm))
        try:
            spot, exp, atm, by = chain(kite)
            sp, C, P = split_now(kite, spot, atm, by)
            res[hhmm] = (spot, exp, atm, by, sp)
            jlog(kind="split", date=X.now().date(), time=hhmm, expiry=exp, spot=spot, atm=atm,
                 split=round(sp, 4) if sp is not None else "", side=side_of(sp), note=f"CE_OI={C} PE_OI={P}")
            X.log(NAME, f"{hhmm} split {sp:.3f} (CE {C:,} / PE {P:,}) spot {spot:.1f} ATM {atm} exp {exp}")
        except Exception as e:
            X.log(NAME, f"{hhmm} snapshot failed: {e}")
    if "15:00" not in res:
        return
    spot, exp, atm, by, sp = res["15:00"]
    side = side_of(sp)
    # 15:00 prices for every leg (so any strike variant can be evaluated later)
    legs = {}
    for ty in ("CE", "PE"):
        for otm in (0, 2, 4):
            k, i = leg(by, atm, ty, otm)
            if i:
                legs[(ty, otm)] = (k, i["tradingsymbol"])
    q = kite.ltp(["NFO:" + s for _, s in legs.values()]) if legs else {}
    px = {key: q.get("NFO:" + s, {}).get("last_price") for key, (k, s) in legs.items()}
    for (ty, otm), (k, s) in legs.items():
        jlog(kind="price1500", date=X.now().date(), time="15:00", expiry=exp, spot=spot, atm=atm,
             split=round(sp or 0, 4), side=side, otm=otm, strike=k, symbol=s, ltp=px[(ty, otm)],
             note="SIGNAL" if ty == side else "")
    if side:
        st = load_state()
        st["open"].append({"date": str(X.now().date()), "expiry": str(exp), "side": side, "split": sp,
                           "spot_in": spot, "atm": atm,
                           "legs": {str(otm): {"strike": legs[(side, otm)][0], "symbol": legs[(side, otm)][1],
                                               "entry": px[(side, otm)]} for otm in (0, 2, 4) if (side, otm) in legs},
                           "status": "open"})
        save_state(st)
        k2, s2 = legs.get((side, 2), (None, None))
        msg = (f"SHADOW signal: call share {sp:.0%} at 15:00 -> would SELL {side}. "
               f"OTM2 {s2} @ {px.get((side, 2))} (no order placed)")
    else:
        msg = f"no signal: call share {sp:.0%} at 15:00 (needs >= 60% or <= 40%)"
    X.log(NAME, msg); X.telegram(cfg, NAME, msg)


def cmd_morning():
    cfg = X.read_cfg()
    st = load_state()
    todo = [p for p in st["open"] if p["status"] == "open" and p["date"] != str(X.now().date())]
    if not todo:
        X.log(NAME, "no open shadow positions"); return
    X.wait_until(X.at("09:12"))
    kite = X.login_retry(NAME, "10:00", "OI-bias shadow morning check")
    X.wait_until(X.at("09:20"))
    spot = kite.ltp(["NSE:NIFTY 50"])["NSE:NIFTY 50"]["last_price"]
    for p in todo:
        if "morning_done" in p:
            continue
        # OI split at 09:20 on the SAME expiry, around today's ATM
        try:
            inst = [i for i in kite.instruments("NFO") if i.get("name") == "NIFTY"
                    and i.get("instrument_type") in ("CE", "PE") and str(i["expiry"]) == p["expiry"]]
            by = {(int(i["strike"]), i["instrument_type"]): i for i in inst}
            atm = int(round(spot / 50.0) * 50)
            sp, C, P = split_now(kite, spot, atm, by)
        except Exception as e:
            sp, C, P = None, 0, 0
            X.log(NAME, f"09:20 split failed: {e}")
        mv = spot - p["spot_in"]
        idx_hold = (mv <= 0) if p["side"] == "CE" else (mv >= 0)
        oi_hold = sp is not None and ((sp > 0.5) if p["side"] == "CE" else (sp < 0.5))
        syms = ["NFO:" + L["symbol"] for L in p["legs"].values()]
        q = kite.ltp(syms) if syms else {}
        for otm, L in p["legs"].items():
            L["ltp_0920"] = q.get("NFO:" + L["symbol"], {}).get("last_price")
            jlog(kind="morning", date=X.now().date(), time="09:20", expiry=p["expiry"], spot=spot, atm=p["atm"],
                 split=round(sp, 4) if sp is not None else "", side=p["side"], otm=otm, strike=L["strike"],
                 symbol=L["symbol"], ltp=L["ltp_0920"],
                 note=f"entry={L['entry']} idx_move={mv:+.1f} idx_hold={idx_hold} oi_hold={oi_hold} signal_date={p['date']}")
        p.update(morning_done=True, spot_0920=spot, split_0920=sp, idx_hold=idx_hold, oi_hold=oi_hold)
        if not idx_hold and not oi_hold:
            p["status"] = "exited_0920"
        save_state(st)
        L2 = p["legs"].get("2", {})
        msg = (f"SHADOW morning ({p['side']} from {p['date']}): Nifty {mv:+.0f} since entry, "
               f"OI call share now {sp:.0%}. Index check: {'HOLD' if idx_hold else 'EXIT'}; "
               f"OI check: {'HOLD' if oi_hold else 'EXIT'}. OTM2 {L2.get('entry')} -> {L2.get('ltp_0920')}"
               if sp is not None else f"SHADOW morning: split unavailable; index check {'HOLD' if idx_hold else 'EXIT'}")
        X.log(NAME, msg); X.telegram(cfg, NAME, msg)


def cmd_settle():
    cfg = X.read_cfg()
    st = load_state()
    today = str(X.now().date())
    due = [p for p in st["open"] if p["status"] == "open" and p["expiry"] <= today and p.get("morning_done")]
    if not due:
        X.log(NAME, "nothing expiring today"); return
    kite = X.login_retry(NAME, "16:30", "OI-bias shadow settle")
    close = kite.ltp(["NSE:NIFTY 50"])["NSE:NIFTY 50"]["last_price"]
    for p in due:
        for otm, L in p["legs"].items():
            k = L["strike"]
            L["settle"] = max(0.0, close - k) if p["side"] == "CE" else max(0.0, k - close)
            jlog(kind="settle", date=today, time="15:30", expiry=p["expiry"], spot=close, side=p["side"], otm=otm,
                 strike=k, symbol=L["symbol"], ltp=round(L["settle"], 2),
                 note=f"entry={L['entry']} signal_date={p['date']} idx_hold={p.get('idx_hold')} oi_hold={p.get('oi_hold')}")
        p["status"] = "settled"
        L2 = p["legs"].get("2", {})
        msg = f"SHADOW settle ({p['side']} from {p['date']}): OTM2 sold {L2.get('entry')}, expiry value {L2.get('settle')}"
        X.log(NAME, msg); X.telegram(cfg, NAME, msg)
    save_state(st)


def cmd_now():
    cfg = X.read_cfg()
    kite = X.kite_login(cfg, NAME)
    spot, exp, atm, by = chain(kite)
    sp, C, P = split_now(kite, spot, atm, by)
    print(f"spot {spot:.1f} ATM {atm} expiry {exp}: call share {sp:.3f} (CE {C:,} / PE {P:,}) -> {side_of(sp) or 'no signal'}")


def cmd_report():
    st = load_state()
    lot = LOT_FALLBACK
    print(f"{'signal':10s} {'side':4s} {'OTM2 entry':>10s} {'09:20':>7s} {'settle':>7s}  idx-rule P&L  oi-rule P&L  status")
    for p in st["open"]:
        L = p["legs"].get("2", {})
        e, m, s = L.get("entry"), L.get("ltp_0920"), L.get("settle")
        def pnl(hold):
            if e is None or m is None:
                return ""
            x = (s if hold else m)
            return "" if x is None else f"{(e - x) * lot:+,.0f}"
        print(f"{p['date']:10s} {p['side']:4s} {e!s:>10s} {m!s:>7s} {s!s:>7s}  {pnl(p.get('idx_hold')):>11s}  "
              f"{pnl(p.get('oi_hold')):>11s}  {p['status']}")


if __name__ == "__main__":
    c = (sys.argv[1:] or ["now"])[0]
    {"snap": cmd_snap, "morning": cmd_morning, "settle": cmd_settle, "now": cmd_now, "report": cmd_report}.get(
        c, lambda: print(__doc__))()
