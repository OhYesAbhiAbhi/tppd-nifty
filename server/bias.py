#!/usr/bin/env python3
"""OI-bias overnight plan (v1, 3 Nov 2026). Runs next to extras.py (v14) and reuses its order/stop/exit code.

Signal (at 15:15): Nifty OI from ATM to ATM+250 (calls) vs ATM to ATM-250 (puts), nearest expiry AFTER today.
  call share >= 0.60 -> bias CE (sell a call);  <= 0.40 -> bias PE (sell a put);  otherwise no bias.

  Wed 15:15  bias    -> sell Nifty OTM2 (100 pts out) on the bias side, next-Tuesday contract   [nf_wed_bias]
                        Thu 11:00: call share still >= 0.55 (CE) / <= 0.45 (PE) -> hold to Thu 15:10 and SKIP sx_thu
                                   else close 11:25 and let sx_thu run at 11:30
             no bias -> Nifty iron fly ATM, wings +-500, 30% stop, exit Thu 09:18             [nfw_fly]
  Fri 15:15  bias    -> sell Nifty OTM2 on the bias side, exit Mon 09:18                       [nf_fri_bias]
             no bias -> Nifty iron fly +-500, 30% stop, exit Mon 09:18                         [nff_fly]
  Fri 15:15  bias    -> sell Sensex OTM2 (200 pts out) on the bias side (Nifty signal), Mon 09:18 [sx_fri_bias]
             no bias -> Sensex iron fly +-1000, 30% stop, every Friday, exit Mon 09:18          [sxf_fly]
  Thu        sx_thu (Sensex 11:30 straddle) now starts via `bias.py sxthu`, which skips it when the Wed bias is held.
Bias legs have no stop (as backtested). Flies use extras.py with first-tick morning stops (as backtested).

Usage (from ~/algo):
  venv/bin/python bias.py check                 signal now + contracts + margin for every path. No orders.
  venv/bin/python bias.py enter wed|fri_nf|fri_sx
  venv/bin/python bias.py exit nfw_fly|nff_fly|sxf_fly|nf_wed_bias|nf_fri_bias|sx_fri_bias
  venv/bin/python bias.py sxthu
  venv/bin/python bias.py status
"""
import json, os, sys, time
from datetime import timedelta

import extras as X
import oi_bias_log as O

BASE = X.BASE
THR, KEEP = 0.60, 0.55
FLY = {   # injected into extras.DEFAULTS so extras' enter/exit/stop code runs them
    "nfw_fly": {"enabled": True, "paper": None, "lots": 1, "kind": "fly", "index": "NIFTY", "weekday": 2,
                "entry_time": "15:15", "exit_time": "09:18", "wing_points": 500, "stop_pct": 30,
                "move_to_cost": False, "min_days": 1, "max_days": 7, "morning_stop": "tick"},
    "nff_fly": {"enabled": True, "paper": None, "lots": 1, "kind": "fly", "index": "NIFTY", "weekday": 4,
                "entry_time": "15:15", "exit_time": "09:18", "wing_points": 500, "stop_pct": 30,
                "move_to_cost": False, "min_days": 1, "max_days": 7, "morning_stop": "tick",
                "index_confirm_pts": 100},   # v16: Monday stop needs Nifty 100 pts against the leg (AlgoTest 23 Nov)
    "sxf_fly": {"enabled": True, "paper": None, "lots": 1, "kind": "fly", "index": "SENSEX", "weekday": 4,
                "entry_time": "15:15", "exit_time": "09:18", "wing_points": 1000, "stop_pct": 30,
                "move_to_cost": False, "min_days": 1, "max_days": 7, "morning_stop": "tick"},
}
BIAS = {  # bias leg: index, weekday, OTM points, matching fly (for event filter / settings), exit rule
    "nf_wed_bias": {"index": "NIFTY", "weekday": 2, "otm": 100, "fly": "nfw_fly", "exit": "thu"},
    "nf_fri_bias": {"index": "NIFTY", "weekday": 4, "otm": 100, "fly": "nff_fly", "exit": "09:18"},
    "sx_fri_bias": {"index": "SENSEX", "weekday": 4, "otm": 200, "fly": "sxf_fly", "exit": "09:18"},
}
ENTRY = {"wed": ("nf_wed_bias", "nfw_fly"), "fri_nf": ("nf_fri_bias", "nff_fly"), "fri_sx": ("sx_fri_bias", "sxf_fly")}
for k, v in FLY.items():
    X.DEFAULTS[k] = v
    if k not in X.NAMES:
        X.NAMES.append(k)


def flag_path(d):
    return os.path.join(BASE, f"skip_sx_thu_{d}.flag")


def sig_path(d):
    return os.path.join(BASE, f"bias_signal_{d}.json")


def signal(kite, use_cache=False):
    """(call share, side, spot, expiry) for Nifty now. Cached per day so Friday's two jobs agree."""
    today = str(X.now().date())
    p = sig_path(today)
    if use_cache:
        for _ in range(20):                           # the Nifty job writes it within seconds of 15:15
            if os.path.exists(p):
                j = json.load(open(p)); return j["split"], j["side"], j["spot"], j["expiry"]
            time.sleep(2)
    spot, exp, atm, by = O.chain(kite)
    sp, C, P = O.split_now(kite, spot, atm, by)
    side = O.side_of(sp)
    tmp = p + ".tmp"
    json.dump({"split": sp, "side": side, "spot": spot, "expiry": str(exp), "ce_oi": C, "pe_oi": P,
               "time": str(X.now())}, open(tmp, "w")); os.replace(tmp, p)
    X.log("bias", f"signal {X.now():%H:%M:%S}: call share {sp:.3f} (CE {C:,} / PE {P:,}) spot {spot:.1f} exp {exp} -> {side or 'no bias'}")
    return sp, side, spot, str(exp)


def bias_contract(kite, idx_name, side, otm):
    ix = X.INDEX[idx_name]
    spot = kite.ltp([ix["spot"]])[ix["spot"]]["last_price"]
    inst = [i for i in kite.instruments(ix["exchange"]) if i.get("name") == ix["name"] and i.get("instrument_type") == side]
    today = X.now().date()
    exp = sorted({i["expiry"] for i in inst if i["expiry"] > today})[0]
    strikes = sorted({i["strike"] for i in inst if i["expiry"] == exp})
    atm = min(strikes, key=lambda x: abs(x - spot))
    target = atm + otm if side == "CE" else atm - otm
    k = min(strikes, key=lambda x: abs(x - target))
    leg = next(i for i in inst if i["expiry"] == exp and i["strike"] == k)
    return ix, spot, exp, atm, leg


def enter_bias(kite, cfg, name, side):
    b = BIAS[name]; s = X.settings(cfg, b["fly"])
    ix, spot, exp, atm, leg = bias_contract(kite, b["index"], side, b["otm"])
    qty = int(leg["lot_size"]) * int(s["lots"]); paper = s["paper"]
    if not paper and not X.margin_gate(kite, cfg, name, s, ix, {"short_bias": leg}, qty, X.now()):
        return
    st = {"status": "opening", "strategy": name, "kind": "bias", "side": side, "date": str(X.now().date()),
          "expiry": str(exp), "spot": spot, "atm": atm, "qty": qty, "paper": paper, "exchange": ix["exchange"], "legs": {}}
    X.save_state(name, st)
    sym = leg["tradingsymbol"]; ref = X.ltps(kite, ix["exchange"], [sym])[sym]
    try:
        fill = X.execute(kite, name, s, ix["exchange"], sym, "SELL", qty, ref, paper)
    except Exception as e:
        held = None if paper else X.net_qty(kite, ix["exchange"], sym)
        if held and held < 0:
            fill = X.last_fill(kite, sym, "SELL") or ref
            X.log(name, f"{sym} filled despite error ({e}) @ {fill}")
        else:
            st["status"] = "failed"; X.save_state(name, st)
            X.log(name, f"ENTRY FAILED {sym}: {e}"); X.telegram(cfg, name, f"⚠️ bias entry failed for {sym}: {e}")
            return
    st["legs"]["short_bias"] = {"symbol": sym, "side": "SELL", "entry": fill, "open": True}
    st["status"] = "open"; X.save_state(name, st)
    msg = (f"{'PAPER ' if paper else ''}BIAS entered: sold {qty} {sym} @ {fill} ({b['index']} {spot:.0f}, "
           f"expiry {exp}); no stop; exit {'Thu 11:00 check' if b['exit'] == 'thu' else 'next day ' + b['exit']}")
    X.log(name, msg); X.telegram(cfg, name, msg)


def cmd_enter(which):
    name, fly = ENTRY[which]
    cfg = X.read_cfg(); s = X.settings(cfg, fly)
    if X.now().weekday() != BIAS[name]["weekday"]:
        X.log(name, "wrong weekday - skip"); return
    if X.paused():
        X.log(name, "paused - skip"); X.telegram(cfg, name, "paused - no entry"); return
    for n in (name, fly):
        old = X.load_state(n)
        if old and old.get("status") in ("open", "opening"):
            X.log(name, f"{n} still open - skip"); X.telegram(cfg, name, f"skip: {n} still open"); return
    if X.event_skip(cfg, name, s):
        return
    X.wait_until(X.at("15:13"))
    kite = X.login_retry(name, "15:15", f"{which} entry at 15:15")
    X.wait_until(X.at("15:15", 20 if which == "fri_sx" else 0))
    if X.paused():
        X.log(name, "paused at entry time - skip"); return
    sp = None
    try:
        sp, side, spot, exp = signal(kite, use_cache=(which == "fri_sx"))
    except Exception as e:
        X.log(name, f"signal failed ({e}) - running the fly"); side = ""
        X.telegram(cfg, name, f"signal failed ({e}); running {fly}")
    if side:
        X.telegram(cfg, name, f"bias {side} (call share {sp:.0%}) -> selling {BIAS[name]['index']} OTM {side}")
        enter_bias(kite, cfg, name, side)
    else:
        if sp is not None:
            X.telegram(cfg, name, f"no bias (call share {sp:.0%}) -> {fly}")
        X.cmd_enter(fly)                               # extras: entry, afternoon stops, holds overnight


def thu_check(kite, st):
    """Call share at 11:00 on the position's own expiry, around today's ATM."""
    spot = kite.ltp(["NSE:NIFTY 50"])["NSE:NIFTY 50"]["last_price"]
    inst = [i for i in kite.instruments("NFO") if i.get("name") == "NIFTY" and i.get("instrument_type") in ("CE", "PE")
            and str(i["expiry"]) == st["expiry"]]
    by = {(int(i["strike"]), i["instrument_type"]): i for i in inst}
    atm = int(round(spot / 50.0) * 50)
    sp, C, P = O.split_now(kite, spot, atm, by)
    keep = sp is not None and (sp >= KEEP if st["side"] == "CE" else sp <= 1 - KEEP)
    return sp, keep, spot


def exit_bias(name):
    cfg = X.read_cfg(); b = BIAS[name]; s = X.settings(cfg, b["fly"])
    st = X.load_state(name)
    if not st or st.get("status") != "open":
        X.log(name, "no open position - nothing to do"); return
    if str(X.now().date()) == st["date"]:
        X.log(name, "exit called on the entry day - nothing to do"); return
    if b["exit"] == "thu":
        X.wait_until(X.at("10:50"))
        kite = X.login_retry(name, "11:20", f"open {name} needs the 11:00 check")
        X.wait_until(X.at("11:00"))
        try:
            sp, keep, spot = thu_check(kite, st)
        except Exception as e:
            sp, keep, spot = None, False, None
            X.log(name, f"11:00 check failed ({e}) - closing at 11:25")
        if keep:
            open(flag_path(X.now().date()), "w").write(f"{st['side']} {sp}\n")
            msg = f"11:00 call share {sp:.0%}: {st['side']} bias holds -> holding to 15:10, Sensex straddle skipped today"
            X.log(name, msg); X.telegram(cfg, name, msg)
            X.wait_until(X.at("15:10"))
        else:
            msg = (f"11:00 call share {sp:.0%}: bias gone -> closing 11:25, Sensex straddle runs 11:30"
                   if sp is not None else "11:00 check unavailable -> closing 11:25, Sensex straddle runs 11:30")
            X.log(name, msg); X.telegram(cfg, name, msg)
            X.wait_until(X.at("11:25"))
    else:
        X.wait_until(X.at("09:05"))
        kite = X.login_retry(name, "11:00", f"open {name} must be closed at {b['exit']}")
        X.wait_until(X.at(b["exit"]))
    X.finish(kite, X.read_cfg(), name, s, st)


def cmd_sxthu():
    cfg = X.read_cfg()
    X.wait_until(X.at("11:27"))
    if os.path.exists(flag_path(X.now().date())):
        X.log("sx_thu", "Wed bias held today - Sensex straddle skipped"); return
    st = X.load_state("nf_wed_bias")
    if st and st.get("status") == "open" and st.get("date") != str(X.now().date()):
        X.wait_until(X.at("11:29"))                    # bias leg is closing at 11:25; give it a moment
    X.cmd_enter("sx_thu")


def cmd_check():
    cfg = X.read_cfg(); kite = X.kite_login(cfg, "check")
    sp, side, spot, exp = signal(kite)
    print(f"Nifty call share now {sp:.3f} -> {side or 'no bias'} (spot {spot:.1f}, expiry {exp})")
    for name, b in BIAS.items():
        s = X.settings(cfg, b["fly"])
        for sd in ("CE", "PE"):
            ix, sspot, sexp, atm, leg = bias_contract(kite, b["index"], sd, b["otm"])
            qty = int(leg["lot_size"]) * int(s["lots"])
            px = X.ltps(kite, ix["exchange"], [leg["tradingsymbol"]])[leg["tradingsymbol"]]
            try:
                m = kite.basket_order_margins([dict(exchange=ix["exchange"], tradingsymbol=leg["tradingsymbol"],
                      transaction_type="SELL", variety="regular", product="NRML", order_type="MARKET",
                      quantity=qty, price=0)], consider_positions=False)["final"]["total"]
            except Exception as e:
                m = f"? ({e})"
            print(f"  {name:12s} {sd}: {leg['tradingsymbol']} qty {qty} ltp {px} margin {m} | {'PAPER' if s['paper'] else 'LIVE'}")
    X.cmd_check("nfw_fly"); X.cmd_check("sxf_fly")


def cmd_status():
    for n in list(BIAS) + list(FLY):
        st = X.load_state(n); print(n, json.dumps(st, indent=1) if st else "no state")


if __name__ == "__main__":
    a = sys.argv[1:] or ["check"]
    if a[0] == "check":
        cmd_check()
    elif a[0] == "status":
        cmd_status()
    elif a[0] == "sxthu":
        cmd_sxthu()
    elif a[0] == "enter" and len(a) > 1 and a[1] in ENTRY:
        cmd_enter(a[1])
    elif a[0] == "exit" and len(a) > 1 and a[1] in BIAS:
        exit_bias(a[1])
    elif a[0] == "exit" and len(a) > 1 and a[1] in FLY:
        X.cmd_exit(a[1])
    else:
        print(__doc__)
