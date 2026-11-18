#!/usr/bin/env python3
"""BTST Ultra (v1, 19 Nov 2026). Runs next to extras.py (v14) and reuses its order / close code.

Every trading day, one run per index on the day's list (default: Sensex every day, plus Nifty on Tuesday):
  09:45        pick the weekly contract (next week's on that index's expiry day) and the strikes 3 steps out of
               the money: Sensex ATM +-300, Nifty ATM +-150. The call and put are separate legs.
  09:45-12:45  track each option's lowest price (the range Low).
  12:45-15:25  sell a leg when its price falls 5% below its range Low (ORB on Low + 5% momentum). A leg whose
               trigger never comes is not traded.
  stop         underlying points from the index level at the leg's fill: Sensex 200, Nifty 60
               (call: index up that much; put: index down that much). Checked every 2 s, also next morning.
  re-entry     once per leg, "at cost": after a stop, sell the same option again when it trades back at or below
               the first sale price (until 15:25 on day 1, then next morning until the exit).
  exit         next trading day 12:30, every open leg.

Order of priority (Tuesday): Sensex legs are handled first on every loop. A Nifty leg is sold only if free margin
covers its own margin plus a reserve for the Sensex legs still waiting to sell; otherwise that Nifty leg is skipped
for the run (Telegram says so). Sensex legs are always sent.

AlgoTest basis (1 lot, net of Rs 20/order, taxes, 0.5% slippage): Sensex 14 Nov 2025 - 14 Nov 2026 +Rs 2,09,575,
Jun 2023 - Nov 2025 +Rs 2,59,678; Nifty Tuesday entries (expiry day) avg +Rs 671/trade. See the project doc
btst-nifty-vs-sensex-by-day.md.

Usage (from ~/algo, with the bot's venv):
  venv/bin/python btst.py run        cron, every 5 min Mon-Fri (does nothing outside 08:55-15:30 IST): one process runs the whole day
                                     (a lock stops duplicates; a restarted process resumes from the state files)
  venv/bin/python btst.py check      dry run: login, today's plan, strikes, prices, margin. No orders.
  venv/bin/python btst.py status     open runs and their legs
Settings: config.json -> "btst" (see DEFAULTS). Pause with the usual PAUSE/STOP file (no new sells; stops and the
12:30 exit still run) or `extras.py skip YYYY-MM-DD "why"` (no new run that day).
"""
import fcntl
import glob
import json
import os
import sys
import time
from datetime import datetime, timedelta

import extras as X

NAME = "btst"
DEFAULTS = {
    "enabled": True,
    "paper": False,
    "days": {"0": ["SENSEX"], "1": ["SENSEX", "NIFTY"], "2": ["SENSEX"], "3": ["SENSEX"], "4": ["SENSEX"]},
    "lots": {"SENSEX": 1, "NIFTY": 1},
    "strike_step": {"SENSEX": 100, "NIFTY": 50},
    "otm_steps": 3,
    "sl_points": {"SENSEX": 200, "NIFTY": 60},
    "start": "09:45",
    "range_end": "12:45",
    "momentum_pct": 5.0,
    "entry_cutoff": "15:25",
    "exit_time": "12:30",
    "reentries": 1,
    "margin_checked": ["NIFTY"],      # these indices sell only if margin allows (after reserving for the others)
    "margin_buffer_pct": 5.0,
    "poll_sec": 2,
}
PRIORITY = {"SENSEX": 0, "NIFTY": 1}
LIVE = ("range", "armed", "ordering", "open", "wait_re")
DIR = X.BASE
JOURNAL_NAME = "btst_journal.csv"


def log(msg):
    X.log(NAME, msg)


def settings(cfg):
    s = json.loads(json.dumps(DEFAULTS))
    for k, v in (cfg.get("btst") or {}).items():
        if isinstance(v, dict) and isinstance(s.get(k), dict):
            s[k].update(v)
        else:
            s[k] = v
    s["limit_buffer_pct"] = float((cfg.get("extras") or {}).get("limit_buffer_pct", 3.0))
    return s


def at(hhmm, t):
    h, m = map(int, hhmm.split(":"))
    return t.replace(hour=h, minute=m, second=0, microsecond=0)


def run_name(index, day):
    return f"btst_{index}_{day:%Y%m%d}"


def load_runs():
    out = []
    for p in sorted(glob.glob(os.path.join(DIR, "state_btst_*_*.json"))):
        try:
            with open(p) as f:
                st = json.load(f)
        except Exception as e:
            log(f"unreadable {p}: {e}"); continue
        if st.get("status") == "active":
            out.append(st)
    return out


def save(st):
    X.save_state(st["name"], st)


def manual_skip(day):
    try:
        return [e for e in X.load_events() if e.get("skip") and e["date"] == str(day)]
    except Exception:
        return []


# ------------------------------------------------------------------ contracts
def pick(kite, s, index, t):
    """Weekly contract (next one on expiry day) and the OTM call/put. Returns the new run dict."""
    ix = X.INDEX[index]
    spot = kite.ltp([ix["spot"]])[ix["spot"]]["last_price"]
    inst = [i for i in kite.instruments(ix["exchange"])
            if i.get("name") == ix["name"] and i.get("instrument_type") in ("CE", "PE")]
    today = t.date()
    exps = sorted({i["expiry"] for i in inst if i["expiry"] >= today})
    if exps and exps[0] == today:
        exps = exps[1:]                                     # expiry day: BTST holds next week's contract
    if not exps:
        raise RuntimeError("no option expiries found")
    exp = exps[0]
    step = float(s["strike_step"][index])
    atm = round(spot / step) * step
    off = step * int(s["otm_steps"])
    legs = {}
    for k, typ, strike in (("ce", "CE", atm + off), ("pe", "PE", atm - off)):
        i = next((i for i in inst if i["expiry"] == exp and i["instrument_type"] == typ
                  and abs(float(i["strike"]) - strike) < 0.01), None)
        if i is None:
            raise RuntimeError(f"no {typ} {strike:.0f} for {exp}")
        legs[k] = {"symbol": i["tradingsymbol"], "type": typ, "strike": strike, "side": "SELL", "phase": "range",
                   "low": None, "trig": None, "open": False, "reentries_left": int(s["reentries"]),
                   "cost": None, "trades": []}
    lot = int(next(i for i in inst if i["tradingsymbol"] == legs["ce"]["symbol"])["lot_size"])
    return {"name": run_name(index, today), "strategy": NAME, "index": index, "date": str(today),
            "exchange": ix["exchange"], "spot_key": ix["spot"], "expiry": str(exp), "spot0": spot, "atm": atm,
            "qty": lot * int(s["lots"][index]), "paper": bool(s["paper"]), "status": "active",
            "sl_points": float(s["sl_points"][index]), "legs": legs, "created": str(t)}


# ------------------------------------------------------------------ margin
def basket(st, keys):
    return [dict(exchange=st["exchange"], tradingsymbol=st["legs"][k]["symbol"], transaction_type="SELL",
                 variety="regular", product="NRML", order_type="MARKET", quantity=st["qty"], price=0) for k in keys]


def margin_ok(kite, cfg, s, st, k, runs, t):
    """True if free margin covers this leg plus the margin still needed by higher-priority runs today."""
    try:
        need = kite.basket_order_margins(basket(st, [k]), consider_positions=True)["final"]["total"]
        reserve = 0.0
        for o in runs:
            if o is st or o["date"] != str(t.date()) or PRIORITY.get(o["index"], 9) >= PRIORITY.get(st["index"], 9):
                continue
            wait = [j for j, L in o["legs"].items() if L["phase"] in ("range", "armed", "wait_re")]
            if wait:
                reserve += kite.basket_order_margins(basket(o, wait), consider_positions=False)["final"]["total"]
        m = kite.margins("equity")
        free = m.get("net") or m["available"]["live_balance"]
    except Exception as e:
        log(f"{st['name']} {k}: margin check failed ({e}) - not selling")
        X.telegram(cfg, st["name"], f"{k.upper()} skipped: margin check failed ({e})")
        return False
    want = (need + reserve) * (1 + float(s["margin_buffer_pct"]) / 100)
    ok = free >= want
    log(f"{st['name']} {k}: margin need Rs {need:,.0f} + reserve Rs {reserve:,.0f}, free Rs {free:,.0f} -> "
        f"{'OK' if ok else 'SKIP'}")
    if not ok:
        X.telegram(cfg, st["name"], f"{k.upper()} {st['legs'][k]['symbol']} not sold: margin short "
                                    f"(need ~Rs {need:,.0f} + Rs {reserve:,.0f} kept for Sensex, free Rs {free:,.0f})")
    return ok


# ------------------------------------------------------------------ leg actions
def held_by_others(runs, st, sym):
    return sum(o["qty"] for o in runs if o is not st for L in o["legs"].values()
               if L["symbol"] == sym and L["open"])


def sell(kite, cfg, s, st, k, spot, ltp, runs, t, why):
    L = st["legs"][k]
    if st["index"] in s["margin_checked"] and not st["paper"] and not margin_ok(kite, cfg, s, st, k, runs, t):
        L["phase"] = "skipped_margin"; save(st); return
    L["prev_phase"] = L["phase"]; L["phase"] = "ordering"; save(st)
    try:
        fill = X.execute(kite, st["name"], s, st["exchange"], L["symbol"], "SELL", st["qty"], ltp, st["paper"])
    except Exception as e:
        fill = None
        if not st["paper"]:
            time.sleep(1)
            held = X.net_qty(kite, st["exchange"], L["symbol"])
            if held is not None and -held >= held_by_others(runs, st, L["symbol"]) + st["qty"]:
                fill = X.last_fill(kite, L["symbol"], "SELL") or ltp
                log(f"{st['name']} {k}: order error ({e}) but Kite shows the sale - entry @ {fill}")
        if fill is None:
            first = L["cost"] is None
            L["phase"] = "failed" if first else "done"
            save(st)
            log(f"{st['name']} {k}: SELL FAILED {L['symbol']}: {e}")
            X.telegram(cfg, st["name"], f"⚠️ could not sell {L['symbol']} ({why}): {e}. Leg dropped for this run.")
            return
    sl = st["sl_points"]
    L.update(open=True, phase="open", entry=fill, entry_time=str(t), spot_in=spot,
             sl_spot=spot + sl if L["type"] == "CE" else spot - sl, entry_why=why)
    if L["cost"] is None:
        L["cost"] = fill
    save(st)
    msg = (f"{'PAPER ' if st['paper'] else ''}SOLD {L['symbol']} @ {fill} ({why}) | {st['index']} {spot:,.0f}, "
           f"stop if {st['index']} {'>=' if L['type'] == 'CE' else '<='} {L['sl_spot']:,.0f} | exit next day "
           f"{s['exit_time']}")
    log(f"{st['name']} {msg}"); X.telegram(cfg, st["name"], msg)


def close(kite, cfg, s, st, k, reason):
    L = st["legs"][k]
    try:
        fill = X.close_leg(kite, cfg, st["name"], s, st, k, reason)
    except Exception as e:
        log(f"{st['name']} {k}: CLOSE FAILED {L['symbol']}: {e}")
        if not L.get("alerted"):
            L["alerted"] = True; save(st)
            X.telegram(cfg, st["name"], f"⚠️ {reason}: could not buy back {L['symbol']} ({e}) - retrying; check Kite")
        return False
    pnl = (L["entry"] - fill) * st["qty"]
    L["trades"].append({"entry": L["entry"], "exit": fill, "entry_time": L["entry_time"], "exit_time": L["exit_time"],
                        "why": L.get("entry_why"), "reason": L["reason"], "pnl": round(pnl, 2)})
    L.pop("alerted", None)
    save(st)
    return True


def step_run(kite, cfg, s, st, px, t, runs, can_sell):
    """One pass over a run's legs at time t. px: dict 'EXCH:SYM' -> last price (spot included)."""
    day1 = st["date"] == str(t.date())
    if not day1 and t >= at(s["exit_time"], t):
        finish(kite, cfg, s, st); return
    spot = px.get(st["spot_key"])
    if spot is None:
        return
    for k in ("ce", "pe"):
        L = st["legs"][k]
        p = px.get(f"{st['exchange']}:{L['symbol']}")
        if p is None:
            continue
        ph = L["phase"]
        if ph == "range":
            if day1 and t < at(s["range_end"], t):
                if L["low"] is None or p < L["low"]:
                    L["low"] = p; st["_dirty"] = True
                continue
            if L["low"] is None:
                L["phase"] = "no_entry"; save(st); continue
            L["trig"] = round(L["low"] * (1 - float(s["momentum_pct"]) / 100), 2)
            L["phase"] = ph = "armed"; save(st)
            log(f"{st['name']} {k}: range low {L['low']} -> sell at <= {L['trig']}")
        if ph == "armed":
            if not day1 or t >= at(s["entry_cutoff"], t):
                L["phase"] = "no_entry"; save(st)
                log(f"{st['name']} {k}: no breakout by {s['entry_cutoff']} - not traded")
            elif p <= L["trig"] and can_sell:
                sell(kite, cfg, s, st, k, spot, p, runs, t, f"breakout {p} <= {L['trig']}")
        elif ph == "open":
            hit = spot >= L["sl_spot"] if L["type"] == "CE" else spot <= L["sl_spot"]
            if hit:
                log(f"{st['name']} STOP {k} {L['symbol']}: {st['index']} {spot} vs {L['sl_spot']}")
                if close(kite, cfg, s, st, k, "stop"):
                    tr = L["trades"][-1]
                    if L["reentries_left"] > 0:
                        L["reentries_left"] -= 1; L["phase"] = "wait_re"
                    else:
                        L["phase"] = "done"
                    save(st)
                    X.telegram(cfg, st["name"], f"stop on {L['symbol']} ({st['index']} {spot:,.0f}): sold {tr['entry']}, "
                                                f"bought back {tr['exit']}, Rs {tr['pnl']:,.0f}"
                                                + (f" | will sell again at <= {L['cost']}" if L["phase"] == "wait_re" else ""))
        elif ph == "wait_re":
            if day1 and t >= at(s["entry_cutoff"], t):
                continue                                    # no re-entry late on day 1; next morning still allowed
            if p <= L["cost"] and can_sell:
                sell(kite, cfg, s, st, k, spot, p, runs, t, f"re-entry at cost {p} <= {L['cost']}")


def end_of_day(s, st, t):
    """15:30 on day 1: legs that never sold are done; a run with nothing open or waiting is closed."""
    for L in st["legs"].values():
        if L["phase"] in ("range", "armed"):
            L["phase"] = "no_entry"
    if not any(L["phase"] in ("open", "wait_re", "ordering") for L in st["legs"].values()):
        st["status"] = "closed"; journal(st, t)
    save(st)


def finish(kite, cfg, s, st):
    for k, L in st["legs"].items():
        if L["open"]:
            if not close(kite, cfg, s, st, k, "time"):
                time.sleep(1); close(kite, cfg, s, st, k, "time")
        if L["phase"] in LIVE and not L["open"]:
            L["phase"] = "done"
    if any(L["open"] for L in st["legs"].values()):
        X.telegram(cfg, st["name"], "⚠️ exit incomplete - check Kite positions; the bot keeps retrying"); save(st)
        return
    st["status"] = "closed"
    pnl = journal(st, X.now())
    save(st)
    msg = f"{'PAPER ' if st['paper'] else ''}closed. gross P&L Rs {pnl:,.0f} before charges | " + "; ".join(
        f"{L['symbol']}: " + (", ".join(f"{x['entry']}->{x['exit']} ({x['reason']})" for x in L["trades"]) or L["phase"])
        for L in st["legs"].values())
    log(f"{st['name']} {msg}"); X.telegram(cfg, st["name"], msg)


def journal(st, t):
    pnl = sum(x["pnl"] for L in st["legs"].values() for x in L["trades"])
    st["gross_pnl"] = round(pnl, 2)
    p = os.path.join(DIR, JOURNAL_NAME)
    new = not os.path.exists(p)
    with open(p, "a") as f:
        if new:
            f.write("run,index,entry_date,exit_date,expiry,qty,paper,trades,gross_pnl,legs\n")
        n = sum(len(L["trades"]) for L in st["legs"].values())
        legs = "|".join(f"{k}:{L['symbol']}:{L['phase']}:" + "/".join(f"{x['entry']}->{x['exit']}:{x['reason']}"
                                                                     for x in L["trades"]) for k, L in st["legs"].items())
        f.write(f"{st['name']},{st['index']},{st['date']},{t.date()},{st['expiry']},{st['qty']},{st['paper']},{n},"
                f"{pnl:.2f},{legs}\n")
    return pnl


def reconcile(kite, cfg, st, runs):
    """After a restart: a leg left in 'ordering' is open if Kite holds the extra short, else back to its old phase."""
    for k, L in st["legs"].items():
        if L["phase"] != "ordering":
            continue
        held = None if st["paper"] else X.net_qty(kite, st["exchange"], L["symbol"])
        if held is not None and -held >= held_by_others(runs, st, L["symbol"]) + st["qty"]:
            fill = X.last_fill(kite, L["symbol"], "SELL") or 0.0
            spot = kite.ltp([st["spot_key"]])[st["spot_key"]]["last_price"]
            sl = st["sl_points"]
            L.update(open=True, phase="open", entry=fill, entry_time=str(X.now()), spot_in=spot,
                     sl_spot=spot + sl if L["type"] == "CE" else spot - sl, entry_why="found in Kite after restart")
            if L["cost"] is None:
                L["cost"] = fill
            X.telegram(cfg, st["name"], f"restart: {L['symbol']} found sold in Kite @ {fill}; stop from {st['index']} {spot:,.0f}")
        else:
            L["phase"] = L.get("prev_phase") or "armed"
        save(st)


# ------------------------------------------------------------------ the day
def todays_indices(s, t):
    return sorted(s["days"].get(str(t.weekday()), []), key=lambda i: PRIORITY.get(i, 9))


def maybe_create(kite, cfg, s, runs, t, warned):
    if not s["enabled"] or X.paused() or manual_skip(t.date()):
        return
    if t < at(s["start"], t) or t >= at(s["range_end"], t):
        return
    for index in todays_indices(s, t):
        name = run_name(index, t.date())
        if os.path.exists(os.path.join(DIR, f"state_{name}.json")) or name in warned:
            continue
        try:
            st = pick(kite, s, index, t)
        except Exception as e:
            log(f"{name}: contract pick failed: {e}")
            if f"{name}:pick" not in warned:
                warned.add(f"{name}:pick"); X.telegram(cfg, name, f"⚠️ could not pick contracts: {e} (retrying)")
            continue
        late = (t - at(s["start"], t)).total_seconds() > 120
        save(st); runs.append(st)
        msg = (f"{'PAPER ' if st['paper'] else ''}BTST {index} {t:%a %d %b}: {index} {st['spot0']:,.0f}, expiry {st['expiry']}, "
               f"watching {st['legs']['ce']['symbol']} / {st['legs']['pe']['symbol']} till {s['range_end']}, "
               f"sell 5% below the range low, stop {st['sl_points']:.0f} {index} pts, exit next day {s['exit_time']}"
               + (" (late start)" if late else ""))
        log(msg); X.telegram(cfg, name, msg)


def quotes(kite, runs):
    keys = sorted({st["spot_key"] for st in runs} | {f"{st['exchange']}:{L['symbol']}" for st in runs
                                                     for L in st["legs"].values()})
    if not keys:
        return {}
    q = kite.ltp(keys)
    return {k: v["last_price"] for k, v in q.items()}


def loop_once(kite, cfg, s, runs, t, warned):
    maybe_create(kite, cfg, s, runs, t, warned)
    active = [st for st in runs if st["status"] == "active"]
    if not active:
        return
    px = quotes(kite, active)
    can_sell = not X.paused()
    for st in sorted(active, key=lambda r: PRIORITY.get(r["index"], 9)):   # Sensex first
        step_run(kite, cfg, s, st, px, t, runs, can_sell)
        if st.pop("_dirty", False) and st["status"] == "active":
            save(st)


def market_open_today(kite, t):
    q = kite.quote(["NSE:NIFTY 50"])["NSE:NIFTY 50"]
    ts = q.get("timestamp") or q.get("last_trade_time")
    if isinstance(ts, str):
        ts = datetime.fromisoformat(ts)
    return ts is None or ts.date() == t.date()


def cmd_run():
    lock = open(os.path.join(DIR, "btst.lock"), "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return                                              # already running
    t = X.now()
    if t.weekday() > 4 or t < at("08:55", t) or t >= at("15:30", t) or os.path.exists(os.path.join(DIR, f"btst_holiday_{t:%Y%m%d}")):
        return
    cfg = X.read_cfg(); s = settings(cfg)
    runs = load_runs()
    if not s["enabled"] and not runs:
        return
    if not runs and t >= at(s["range_end"], t):
        return                                              # nothing to manage and too late to start today's
    kite = X.login_retry(NAME, "15:00", "BTST Ultra" + (" has open positions" if runs else ""))
    X.wait_until(at("09:15", X.now()) + timedelta(seconds=20))
    t = X.now()
    for _ in range(5):                                      # the first index tick can lag the bell
        if market_open_today(kite, t):
            break
        time.sleep(60); t = X.now()
    else:
        open(os.path.join(DIR, f"btst_holiday_{t:%Y%m%d}"), "w").write("no trading today\n")
        log("market closed today - nothing to do"); return
    for st in runs:
        reconcile(kite, cfg, st, runs)
    log(f"running: {len(runs)} open run(s) " + ", ".join(st["name"] for st in runs))
    warned, last_cfg, errs = set(), time.time(), 0
    while True:
        t = X.now()
        if t >= at("15:30", t):
            for st in runs:
                if st["status"] == "active" and st["date"] == str(t.date()):
                    end_of_day(s, st, t)
            open_n = sum(1 for st in runs if st["status"] == "active")
            log(f"15:30 - done for the day; {open_n} run(s) held overnight"); return
        if time.time() - last_cfg > 60:
            try:
                cfg = X.read_cfg(); s = settings(cfg)
            except Exception as e:
                log(f"config re-read failed: {e}")
            last_cfg = time.time()
        try:
            loop_once(kite, cfg, s, runs, t, warned); errs = 0
        except Exception as e:
            errs += 1
            log(f"loop error: {e}")
            if errs in (5, 60):
                X.telegram(cfg, NAME, f"⚠️ BTST loop errors ({e}) - still retrying")
            time.sleep(3)
        runs[:] = [st for st in runs if st["status"] == "active" or st["date"] == str(t.date())]
        time.sleep(float(s["poll_sec"]))


def cmd_check():
    cfg = X.read_cfg(); s = settings(cfg)
    kite = X.kite_login(cfg, NAME)
    t = X.now()
    print(f"enabled={s['enabled']} paper={s['paper']} | days: " +
          ", ".join(f"{X.DAYS[int(d)]} {'+'.join(v)}" for d, v in sorted(s["days"].items())))
    for index in sorted({i for v in s["days"].values() for i in v}, key=lambda i: PRIORITY.get(i, 9)):
        try:
            st = pick(kite, s, index, t)
        except Exception as e:
            print(f"{index}: contract pick failed: {e}"); continue
        px = quotes(kite, [st])
        print(f"{index} {st['spot0']:,.1f} | expiry {st['expiry']} | qty {st['qty']} | stop {st['sl_points']:.0f} pts")
        for k, L in st["legs"].items():
            print(f"   {k} {L['symbol']:24s} LTP {px.get(st['exchange'] + ':' + L['symbol'])}")
        try:
            need = kite.basket_order_margins(basket(st, ["ce", "pe"]), consider_positions=True)["final"]["total"]
            print(f"   margin for both legs ~Rs {need:,.0f}")
        except Exception as e:
            print(f"   margin check failed: {e}")
    try:
        m = kite.margins("equity"); print(f"free margin Rs {m.get('net') or m['available']['live_balance']:,.0f}")
    except Exception:
        pass
    print(f"open runs: {[st['name'] for st in load_runs()] or 'none'} | pause files: {X.paused() or 'none'} - no orders placed")


def cmd_status():
    runs = load_runs()
    if not runs:
        print("no open BTST runs")
    for st in runs:
        print(st["name"], st["expiry"], "qty", st["qty"])
        for k, L in st["legs"].items():
            print(f"  {k} {L['symbol']} {L['phase']} entry={L.get('entry')} stop_at={L.get('sl_spot')} trades={L['trades']}")


if __name__ == "__main__":
    a = sys.argv[1:] or ["check"]
    {"run": cmd_run, "check": cmd_check, "status": cmd_status}.get(a[0], lambda: print(__doc__))()
