"""
Nifty option-selling bot for Zerodha Kite Connect  (LIVE or PAPER mode).

Strategy: at 09:20 sell the ATM call + ATM put (or a strangle), place a stop-loss BUY order at the exchange for each
leg (+30% of entry), cut everything if the day's loss crosses max_daily_loss_rs, buy back what's open at 15:15.

Modes (config.json "paper"): true  -> orders are simulated against live Kite prices, nothing real is placed.
                              false -> real orders.
Safety rails:
  * Exchange-side SL order on every sold leg (live), so stops work even if this script/server dies.
  * Daily loss cap, kill switch (file STOP or the Pause button on the phone page), restart-safe state file,
    incomplete entry -> flatten and stand aside, LIMIT / SL-LIMIT orders only.
Learning:
  * journal.csv  - one row per day: context (gap, VIX, DTE), fills, stop slippage, best/worst MTM, charges, net P&L.
  * shadow.csv   - same day's result for several rule variations tracked on the same prices (never traded).
  * Telegram     - entry, stops, exit and an end-of-day summary.

Run:   python straddle_live.py            (mode from config)
       python straddle_live.py --check    (prints what it WOULD sell right now, places nothing)
"""
import json, os, sys, time, math, csv, logging, datetime as dt
import socket, urllib3.util.connection as _u3c
_u3c.allowed_gai_family = lambda: socket.AF_INET   # force IPv4: Kite only accepts orders from the whitelisted static IPv4
from kiteconnect import KiteConnect
from market import Market, last, bid, ask
from notify import tg, charges
import shadow

HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)
CFG = json.load(open("config.json"))
PAPER = bool(CFG.get("paper", False))
MODE = "PAPER" if PAPER else "LIVE"
TODAY = dt.date.today()
STATE_FILE = f"state_{TODAY:%Y%m%d}.json"
TICK = 0.05

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                    handlers=[logging.FileHandler(f"log_{TODAY:%Y%m%d}.txt"), logging.StreamHandler()])
log = logging.getLogger("bot")
MKT = None

# ---------------------------------------------------------------- helpers
def now(): return dt.datetime.now()
def hhmm(): return now().strftime("%H:%M:%S")
def at(s): h, m = map(int, s.split(":")); return dt.datetime.combine(TODAY, dt.time(h, m))
def tick_up(x): return round(math.ceil(round(x / TICK, 6)) * TICK, 2)
def tick_down(x): return round(max(TICK, math.floor(round(x / TICK, 6)) * TICK), 2)
def load_state(): return json.load(open(STATE_FILE)) if os.path.exists(STATE_FILE) else {"phase": "none", "legs": []}
def save_state(s): json.dump(s, open(STATE_FILE + ".tmp", "w"), indent=2, default=str); os.replace(STATE_FILE + ".tmp", STATE_FILE)
def kill_switch(): return os.path.exists(os.path.join(HERE, "STOP"))
def alert(msg): log.info("TG: " + msg.replace("\n", " | ")); tg(CFG, f"[{MODE}] " + msg)

def connect():
    global MKT
    real = KiteConnect(api_key=CFG["api_key"])
    real.set_access_token(open("access_token.txt").read().strip())
    real.profile()   # fails fast if today's login wasn't done
    MKT = Market(real)
    if PAPER:
        from paper import PaperKite
        return PaperKite(real, MKT, f"paper_orders_{TODAY:%Y%m%d}.json", log)
    return real

def orders_by_id(kite): return {o["order_id"]: o for o in kite.orders()}

def ltp(kite, syms):
    q = MKT.snap(extra=[f"NFO:{s}" for s in syms])
    return {s: last(q, f"NFO:{s}") for s in syms}

def place(kite, sym, side, qty, price, order_type="LIMIT", trigger=None):
    params = dict(variety=kite.VARIETY_REGULAR, exchange="NFO", tradingsymbol=sym, transaction_type=side,
                  quantity=qty, product=CFG["product"], order_type=order_type, price=price, validity="DAY")
    if trigger is not None: params["trigger_price"] = trigger
    oid = kite.place_order(**params)
    log.info(f"ORDER {side} {sym} qty={qty} {order_type} price={price} trig={trigger} -> {oid}")
    return oid

def wait_fill(kite, oid, sym, side, qty, timeout=90, chase_every=10, chase_pct=0.005):
    """Wait for a LIMIT order to fill, nudging the price towards the market. Returns (filled_qty, avg_price)."""
    t0 = time.time(); lastc = t0
    while time.time() - t0 < timeout:
        o = orders_by_id(kite).get(oid)
        if o and o["status"] == "COMPLETE": return o["filled_quantity"], o["average_price"]
        if o and o["status"] in ("REJECTED", "CANCELLED"):
            log.error(f"{sym} order {oid} {o['status']}: {o.get('status_message')}")
            alert(f"Order {o['status']} for {sym}: {o.get('status_message')}")
            return o.get("filled_quantity", 0), o.get("average_price", 0)
        if time.time() - lastc >= chase_every and o:
            px = ltp(kite, [sym])[sym]
            new = tick_down(px * (1 - chase_pct)) if side == "SELL" else tick_up(px * (1 + chase_pct))
            try: kite.modify_order(variety=kite.VARIETY_REGULAR, order_id=oid, price=new); log.info(f"chase {sym} -> {new}")
            except Exception as e: log.warning(f"modify failed {e}")
            lastc = time.time()
        time.sleep(1)
    o = orders_by_id(kite).get(oid, {})
    if o.get("status") != "COMPLETE":
        try: kite.cancel_order(variety=kite.VARIETY_REGULAR, order_id=oid)
        except Exception: pass
        time.sleep(1); o = orders_by_id(kite).get(oid, {})
    return o.get("filled_quantity", 0), o.get("average_price", 0)

# ---------------------------------------------------------------- instrument selection
WEEK = {}   # current-week contract info, used by the day-filter shadow

def chain_for(kite):
    inst = [i for i in kite.instruments("NFO") if i["name"] == "NIFTY" and i["segment"] == "NFO-OPT"]
    expiries = sorted({i["expiry"] for i in inst if i["expiry"] >= TODAY})
    exp = expiries[0]
    WEEK.update(exp=expiries[0], table={(i["instrument_type"], int(i["strike"])): i for i in inst if i["expiry"] == expiries[0]})
    if exp == TODAY and CFG.get("use_next_expiry_on_expiry_day", True): exp = expiries[1]
    table = {(i["instrument_type"], int(i["strike"])): i for i in inst if i["expiry"] == exp}
    return exp, table

def pick_legs(kite):
    q = MKT.snap()
    spot = last(q, "NSE:NIFTY 50")
    atm = int(round(spot / 50) * 50)
    exp, table = chain_for(kite)
    off = 0 if CFG["strategy"] == "straddle" else int(CFG["strangle_offset"])
    legs = []
    for typ, k in (("CE", atm + off), ("PE", atm - off)):
        i = table.get((typ, k))
        if not i: raise RuntimeError(f"Could not find {typ} {k} expiry {exp}")
        legs.append(dict(symbol=i["tradingsymbol"], kind=typ, strike=k))
    lot = table[("CE", atm + off)]["lot_size"]
    lookup = lambda typ, k: table[(typ, k)]["tradingsymbol"]
    return spot, atm, exp, lot, legs, lookup

# ---------------------------------------------------------------- entry
def enter(kite, state, live=True, why="paused (STOP) - shadows only"):
    spot, atm, exp, lot, legs, lookup = pick_legs(kite)
    qty = lot * int(CFG["lots"])
    q = MKT.snap(extra=[f"NFO:{l['symbol']}" for l in legs], max_age=0)
    n, v = q["NSE:NIFTY 50"], q["NSE:INDIA VIX"]
    prev_c = n["ohlc"]["close"]; vix_prev = v["ohlc"]["close"]
    ctx = dict(entry_time=hhmm(), spot_entry=spot, nifty_prev_close=prev_c, nifty_open=n["ohlc"]["open"],
               gap_pct=round((n["ohlc"]["open"] / prev_c - 1) * 100, 2) if prev_c else None,
               vix=last(q, "NSE:INDIA VIX"), vix_chg_pct=round((last(q, "NSE:INDIA VIX") / vix_prev - 1) * 100, 2) if vix_prev else None,
               expiry=str(exp), dte=(exp - TODAY).days, atm=atm, lot=lot, qty=qty)
    state.update(phase="entering", exit_reason="", ctx=ctx, expiry=str(exp), qty=qty, legs=[], best=0, worst=0, best_t=None, worst_t=None)
    # shadows start on the same snapshot, independent of whether live orders fill
    if state.get("shadow"):                               # shadows already running since 9:20
        pass
    else:
      try:
          day = day_context(atm)
          snap = _shadow_snap(lookup, atm, extra=day.pop("_keys", []))
          day["flags"] = shadow.day_flags(snap, lookup, atm, spot, day); day.pop("week_lookup", None)   # keep state JSON-safe
          ctx["day_flags"] = "; ".join(day["flags"]) or "none"
          log.info(f"Day filter: DTE(week) {day['dte_week']}  VIX prev {day.get('vix_prev')} / {day.get('vix_prev2')}  "
                   f"premium/day {day.get('implied_daily')}%  flags: {ctx['day_flags']}")
          state["shadow"] = shadow.start(snap, lookup, atm, qty, ctx["vix_chg_pct"], hist_bars=nifty_hist_bars(), day=day)
          MKT.add(shadow.keys(state["shadow"]))
      except Exception as e:
          log.warning(f"shadow start failed: {e}"); state["shadow"] = None
    save_state(state)
    log.info(f"[{MODE}] Spot {spot:.1f}  ATM {atm}  expiry {exp}  lot {lot}  qty {qty}  VIX {ctx['vix']} ({ctx['vix_chg_pct']}%)  gap {ctx['gap_pct']}%")
    log.info(f"Available margin Rs {kite.margins('equity')['net']:,.0f}")
    if not live:                                           # paused day: no orders at all, shadows still tracked
        state.update(phase="shadow_only", exit_reason=why); save_state(state); return

    for l in legs:
        key = f"NFO:{l['symbol']}"
        px = tick_down(bid(q, key) * (1 - CFG["entry_slippage_pct"]))
        l["sell_oid"] = place(kite, l["symbol"], "SELL", qty, px)
        state["legs"].append(l); save_state(state)

    for l in state["legs"]:
        l["filled"], l["avg"] = wait_fill(kite, l["sell_oid"], l["symbol"], "SELL", qty)
        l["closed"] = l["filled"] == 0; save_state(state)

    filled = [l for l in state["legs"] if l["filled"] > 0]
    if len(filled) < 2 or any(l["filled"] != qty for l in filled):
        log.error("Entry incomplete - closing whatever filled and standing aside today.")
        alert("Entry incomplete - closing whatever filled, no trade today.")
        exit_all(kite, state, reason="incomplete entry"); return

    for l in state["legs"]:
        place_sl(kite, l); save_state(state)
    credit = sum(l["avg"] for l in state["legs"])
    state["phase"] = "entered"; save_state(state)
    alert("Sold " + " + ".join(f"{l['symbol']} @ {l['avg']} (SL {l['sl_trigger']})" for l in state["legs"]) +
          f"\nCredit {credit:.2f} pts = Rs {credit*qty:,.0f}  |  Nifty {spot:.0f}, VIX {ctx['vix']}")

def nifty_hist_bars():
    """Previous days' 15-minute Nifty candles, to warm up the Supertrend used by the trend-exit shadows."""
    try:
        c = MKT.k.historical_data(256265, now() - dt.timedelta(days=8), now(), "15minute")
        bars = [[x["open"], x["high"], x["low"], x["close"]] for x in c if x["date"].date() < TODAY]
        log.info(f"Loaded {len(bars)} historical 15m Nifty bars for shadow indicators")
        return bars[-150:]
    except Exception as e:
        log.warning(f"historical bars unavailable ({e}); trend shadows start cold")
        return []

def day_context(atm):
    """Inputs for the day-filter shadow: previous two VIX closes and the current-week ATM straddle."""
    day = dict(expiry_day=WEEK.get("exp") == TODAY, dte_week=(WEEK["exp"] - TODAY).days if WEEK.get("exp") else None)
    try:
        c = [x for x in MKT.k.historical_data(264969, now() - dt.timedelta(days=12), now(), "day") if x["date"].date() < TODAY]
        day["vix_prev"], day["vix_prev2"] = c[-1]["close"], c[-2]["close"]
    except Exception as e:
        log.warning(f"VIX daily history unavailable ({e})")
    tb = WEEK.get("table", {})
    if ("CE", atm) in tb and ("PE", atm) in tb:
        day["week_lookup"] = lambda typ, k: tb[(typ, k)]["tradingsymbol"]
        day["_keys"] = ["NFO:" + tb[("CE", atm)]["tradingsymbol"], "NFO:" + tb[("PE", atm)]["tradingsymbol"]]
    return day

def _shadow_snap(lookup, atm, extra=()):
    keys = list(extra)
    for v in shadow.VARIANTS:
        for typ, k in (("CE", atm + v["off"]), ("PE", atm - v["off"])):
            keys.append("NFO:" + lookup(typ, k))
    return MKT.snap(extra=keys, max_age=0)

def place_sl(kite, l):
    trig = tick_up(l["avg"]) if l.get("at_cost") else tick_up(l["avg"] * (1 + CFG["sl_pct"]))
    limit = tick_up(trig * (1 + CFG["sl_limit_buffer_pct"]))
    l["sl_trigger"], l["sl_limit"] = trig, limit
    l["sl_oid"] = place(kite, l["symbol"], "BUY", l["filled"], limit, order_type="SL", trigger=trig)


def move_to_cost(kite, state, stopped):
    """After one leg's stop fills, move the other open leg's exchange SL to its entry price.
    Backtest 2 Nov 2026 (Mondays 11:30, S30 + cost): +27.8k, DD -2.7k vs S30 +22.5k, DD -4.5k.
    If the modify fails, the original 30% stop stays in place and Telegram says so."""
    for o in state["legs"]:
        if o is stopped or o.get("closed") or o.get("at_cost") or not o.get("sl_oid"):
            continue
        try:
            px = ltp(kite, [o["symbol"]])[o["symbol"]]
            trig = tick_up(o["avg"])
            if px >= trig:                     # already above cost: SL-buy trigger must sit above LTP, so exit next tick
                trig = tick_up(px + TICK)
            limit = tick_up(trig * (1 + CFG["sl_limit_buffer_pct"]))
            kite.modify_order(variety=kite.VARIETY_REGULAR, order_id=o["sl_oid"], trigger_price=trig, price=limit)
            o.update(sl_trigger=trig, sl_limit=limit, at_cost=True); save_state(state)
            log.warning(f"MOVE TO COST {o['symbol']}: SL trigger -> {trig} (entry {o['avg']})")
            alert(f"Move to cost: {o['symbol']} stop moved to {trig} (sold at {o['avg']})")
        except Exception as e:
            log.exception(f"move to cost failed for {o['symbol']}: {e}")
            alert(f"⚠️ Could not move {o['symbol']} stop to cost ({e}). The 30% stop at {o.get('sl_trigger')} is still in place.")

# ---------------------------------------------------------------- exit
def live_exit_time():

    """Per-weekday exit for the live trade (config exit_time_by_day, e.g. {"0": "15:10"}); shadows keep exit_time."""

    return CFG.get("exit_time_by_day", {}).get(str(now().weekday()), CFG["exit_time"])





def exit_all(kite, state, reason):
    log.warning(f"EXIT ALL: {reason}")
    state["phase"] = "exiting"; state["exit_reason"] = reason; save_state(state)
    for l in state["legs"]:
        if l.get("closed") or not l.get("filled"): continue
        sl = l.get("sl_oid"); o = {}
        if sl:
            o = orders_by_id(kite).get(sl, {})
            if o.get("status") == "COMPLETE":
                l.update(closed=True, exit=o["average_price"], why="stop-loss", exit_t=hhmm()); save_state(state); continue
            try: kite.cancel_order(variety=kite.VARIETY_REGULAR, order_id=sl)
            except Exception as e: log.warning(f"cancel SL {sl}: {e}")
            time.sleep(1)
            o = orders_by_id(kite).get(sl, {})
            if o.get("status") == "COMPLETE":
                l.update(closed=True, exit=o["average_price"], why="stop-loss", exit_t=hhmm()); save_state(state); continue
            done = o.get("filled_quantity", 0) or 0
        else:
            done = 0
        remaining = l["filled"] - done
        if remaining <= 0: l.update(closed=True, exit=o.get("average_price"), why=reason, exit_t=hhmm()); save_state(state); continue
        px = tick_up(ltp(kite, [l["symbol"]])[l["symbol"]] * (1 + CFG["exit_slippage_pct"]))
        oid = place(kite, l["symbol"], "BUY", remaining, px)
        l["exit_oid"] = oid; save_state(state)
        qf, avg = wait_fill(kite, oid, l["symbol"], "BUY", remaining, timeout=120, chase_every=5, chase_pct=0.02)
        if qf < remaining:
            log.critical(f"!!! {l['symbol']}: could only buy back {qf}/{remaining}. CLOSE IT MANUALLY IN KITE NOW !!!")
            alert(f"!!! Could only buy back {qf}/{remaining} of {l['symbol']}. CLOSE IT MANUALLY IN KITE NOW !!!")
        l.update(closed=qf >= remaining, exit=avg, why=reason, exit_t=hhmm()); save_state(state)
    state["phase"] = "exited"; save_state(state)
    alert(f"Position closed ({reason}). Live P&L so far: Rs {realised(state):,.0f}")

def realised(state):
    return round(sum((l["avg"] - (l.get("exit") or 0)) * l["filled"] for l in state["legs"] if l.get("filled") and l.get("closed")))

# ---------------------------------------------------------------- monitor
def track_shadow(state, q, final=False):
    if not state.get("shadow"): return
    try:
        shadow.update(state["shadow"], q, CFG["max_daily_loss_rs"], final=final)
        MKT.add(shadow.keys(state["shadow"]))            # late-entry shadows may ask for new strikes
    except Exception as e: log.warning(f"shadow update: {e}")

def monitor(kite, state):
    last_beat = 0; triggered_since = {}; fast = False
    while True:
        if kill_switch(): return exit_all(kite, state, "kill switch (STOP)")
        if now() >= at(live_exit_time()): return exit_all(kite, state, f"{live_exit_time()} exit")
        try:
            od = orders_by_id(kite)
            open_legs = []
            for l in state["legs"]:
                if l.get("closed"): continue
                o = od.get(l["sl_oid"], {}); st = o.get("status")
                if st == "COMPLETE":
                    l.update(closed=True, exit=o["average_price"], why="stop-loss", exit_t=hhmm()); save_state(state)
                    slip = round(o["average_price"] - l["sl_trigger"], 2)
                    log.warning(f"STOP-LOSS HIT {l['symbol']} bought back at {o['average_price']} (slippage {slip})")
                    alert(f"Stop-loss hit: {l['symbol']} bought back at {o['average_price']} (trigger {l['sl_trigger']}, slippage {slip} pts). Booked Rs {(l['avg']-o['average_price'])*l['filled']:,.0f}")
                    if TODAY.weekday() in CFG.get("move_to_cost_days", []): move_to_cost(kite, state, l)
                    continue
                if st in ("REJECTED", "CANCELLED"):
                    log.error(f"SL order for {l['symbol']} is {st} ({o.get('status_message')}). Re-placing.")
                    alert(f"SL order for {l['symbol']} was {st} - re-placing it.")
                    place_sl(kite, l); save_state(state)
                if st == "OPEN":
                    triggered_since.setdefault(l["sl_oid"], time.time())
                    if time.time() - triggered_since[l["sl_oid"]] > CFG.get("sl_chase_seconds", 2):
                        new = tick_up(ltp(kite, [l["symbol"]])[l["symbol"]] * 1.03)
                        kite.modify_order(variety=kite.VARIETY_REGULAR, order_id=l["sl_oid"], price=new)
                        log.warning(f"SL {l['symbol']} triggered but unfilled - repriced to {new}")
                        triggered_since[l["sl_oid"]] = time.time()
                open_legs.append(l)

            q = MKT.snap(extra=[f"NFO:{l['symbol']}" for l in state["legs"]])
            track_shadow(state, q)
            mtm = sum((l["avg"] - (l["exit"] if l.get("closed") else last(q, f"NFO:{l['symbol']}"))) * l["filled"] for l in state["legs"] if l.get("filled"))
            if mtm > state["best"]: state["best"], state["best_t"] = round(mtm), hhmm()
            if mtm < state["worst"]: state["worst"], state["worst_t"] = round(mtm), hhmm()
            state["mtm"] = round(mtm); save_state(state)
            if not open_legs:
                log.info("Both legs stopped out."); state["phase"] = "exited"; state["exit_reason"] = "both stopped"; save_state(state)
                alert(f"Both legs stopped out. Live P&L: Rs {realised(state):,.0f}"); return
            near = any(last(q, f"NFO:{l['symbol']}") >= l["avg"] + 0.75 * (l["sl_trigger"] - l["avg"]) for l in open_legs)
            near = near or mtm <= -0.8 * abs(CFG["max_daily_loss_rs"])
            if near != fast:
                fast = near
                log.info(f"Near stop-loss: polling every {CFG.get('fast_poll_seconds', 1.2)}s" if near else "Back to normal polling")
            if mtm <= -abs(CFG["max_daily_loss_rs"]):
                return exit_all(kite, state, f"daily loss cap (MTM Rs {mtm:,.0f})")
            if time.time() - last_beat > 300:
                log.info(f"MTM Rs {mtm:,.0f}  " + "  ".join(f"{l['symbol']}={last(q, 'NFO:'+l['symbol'])}" for l in open_legs)); last_beat = time.time()
        except Exception as e:
            log.exception(f"monitor error (SL orders at exchange still protect you): {e}")
        time.sleep(CFG.get("fast_poll_seconds", 1.2) if fast else CFG["poll_seconds"])

def shadow_until_close(state):
    """Keep tracking the shadow variants until 15:15 even after the live trade is done."""
    if not state.get("shadow"): return
    while now() < at(CFG["exit_time"]):
        try: track_shadow(state, MKT.snap()); save_state(state)
        except Exception as e: log.warning(f"shadow loop: {e}")
        if all(d["done"] for d in state["shadow"]["variants"]): return
        time.sleep(CFG["poll_seconds"])
    try: track_shadow(state, MKT.snap(max_age=0), final=True); save_state(state)
    except Exception as e: log.warning(f"shadow final: {e}")

# ---------------------------------------------------------------- end of day
JOURNAL_COLS = ["date", "mode", "entry_time", "spot_entry", "nifty_prev_close", "nifty_open", "gap_pct", "vix", "vix_chg_pct",
                "expiry", "dte", "atm", "qty", "ce_symbol", "ce_entry", "ce_sl", "ce_exit", "ce_why", "ce_exit_t", "ce_slippage",
                "pe_symbol", "pe_entry", "pe_sl", "pe_exit", "pe_why", "pe_exit_t", "pe_slippage", "credit_pts",
                "gross_pnl", "charges", "net_pnl", "best_mtm", "best_t", "worst_mtm", "worst_t",
                "day_high", "day_low", "day_close", "exit_reason"]

def end_of_day(state):
    if state.get("eod_done"): return
    ctx = state.get("ctx", {}); qty = state.get("qty", 0)
    row = {k: "" for k in JOURNAL_COLS}; row.update(date=str(TODAY), mode=MODE, exit_reason=state.get("exit_reason", ""))
    row.update({k: ctx.get(k, "") for k in JOURNAL_COLS if k in ctx})
    sv = bv = 0; n_orders = 0
    for l in state["legs"]:
        p = "ce" if l["kind"] == "CE" else "pe"
        if not l.get("filled"): continue
        slip = round(l["exit"] - l["sl_trigger"], 2) if l.get("why") == "stop-loss" and l.get("sl_trigger") and l.get("exit") else ""
        row.update({f"{p}_symbol": l["symbol"], f"{p}_entry": l["avg"], f"{p}_sl": l.get("sl_trigger", ""), f"{p}_exit": l.get("exit", ""),
                    f"{p}_why": l.get("why", ""), f"{p}_exit_t": l.get("exit_t", ""), f"{p}_slippage": slip})
        sv += l["avg"] * l["filled"]; bv += (l.get("exit") or 0) * l["filled"]; n_orders += 2
    gross = realised(state); ch = charges(sv, bv, n_orders) if n_orders else 0
    row.update(credit_pts=round(sum(l.get("avg", 0) for l in state["legs"] if l.get("filled")), 2),
               gross_pnl=gross, charges=round(ch), net_pnl=round(gross - ch),
               best_mtm=state.get("best", ""), best_t=state.get("best_t", ""), worst_mtm=state.get("worst", ""), worst_t=state.get("worst_t", ""))
    try:
        n = MKT.snap(max_age=0)["NSE:NIFTY 50"]
        row.update(day_high=n["ohlc"]["high"], day_low=n["ohlc"]["low"], day_close=n["last_price"])
    except Exception: pass
    new = not os.path.exists("journal.csv")
    with open("journal.csv", "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=JOURNAL_COLS)
        if new: w.writeheader()
        w.writerow(row)
    srows = shadow.results(state["shadow"]) if state.get("shadow") else []
    if srows:
        new = not os.path.exists("shadow.csv")
        with open("shadow.csv", "a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["date", "mode", "variant", "gross", "charges", "net", "detail"])
            if new: w.writeheader()
            for r in srows: w.writerow(dict(date=str(TODAY), mode=MODE, **r))
    state["eod_done"] = True; save_state(state)
    lines = [f"Day summary {TODAY:%d %b}: net Rs {row['net_pnl']:,} (gross {gross:,}, charges {round(ch)})",
             f"Nifty {ctx.get('spot_entry','')} -> {row.get('day_close','')}, range {row.get('day_low','')}-{row.get('day_high','')}, VIX {ctx.get('vix','')} ({ctx.get('vix_chg_pct','')}%)",
             f"Best {row['best_mtm']} @ {row['best_t']}, worst {row['worst_mtm']} @ {row['worst_t']}"]
    for l in state["legs"]:
        if l.get("filled"): lines.append(f"{l['symbol']}: {l['avg']} -> {l.get('exit')} ({l.get('why')} {l.get('exit_t','')})")
    if srows:
        lines.append("Shadow variants (net Rs):")
        lines += [f"  {r['variant']}: {r['net'] if r['net'] is not None else '-'}" for r in srows]
    alert("\n".join(lines))

# ---------------------------------------------------------------- weekday schedule (decided 25 Oct 2026)
# Expiry day: 09:20 (next week's contract). Monday: 11:30. Wed-Fri: 10:45 only if VIX <= its 9:15 open, else sit out.
DEFAULT_SCHEDULE = {"expiry": {"time": "09:20"}, "0": {"time": "11:30"}, "1": {"time": "09:20"},
                    "2": {"time": "10:45", "vix_calm": True}, "3": {"time": "10:45", "vix_calm": True}, "4": {"time": "10:45", "vix_calm": True}}

def todays_plan(kite):
    sch = CFG.get("schedule", DEFAULT_SCHEDULE)
    try:
        inst = [i for i in kite.instruments("NFO") if i["name"] == "NIFTY" and i["segment"] == "NFO-OPT"]
        expiry_today = min(i["expiry"] for i in inst if i["expiry"] >= TODAY) == TODAY
    except Exception as e:
        log.warning(f"could not read expiry ({e}); using weekday rule"); expiry_today = False
    p = dict(sch["expiry"] if expiry_today else sch.get(str(TODAY.weekday()), {"time": CFG["entry_time"]}))
    p["label"] = "expiry day" if expiry_today else TODAY.strftime("%A")
    return p

def vix_calm():
    q = MKT.snap(max_age=0)["NSE:INDIA VIX"]
    v, o = q["last_price"], q["ohlc"]["open"]
    return v <= o, v, o

def wait_with_shadows(until):
    """Track shadows while waiting for the live entry time. Returns False if STOP appears."""
    while now() < until:
        if kill_switch(): return False
        try: track_shadow(state_ref[0], MKT.snap()); save_state(state_ref[0])
        except Exception as e: log.warning(f"shadow wait: {e}")
        time.sleep(CFG["poll_seconds"])
    return not kill_switch()
state_ref = [None]

# ---------------------------------------------------------------- main
def main():
    kite = connect()
    if "--check" in sys.argv:
        spot, atm, exp, lot, legs, _ = pick_legs(kite)
        p = ltp(kite, [l["symbol"] for l in legs])
        print(f"[{MODE}] Spot {spot}  ATM {atm}  expiry {exp}  lot {lot}  lots {CFG['lots']}")
        for l in legs: print(f"  would SELL {l['symbol']} @ ~{p[l['symbol']]}  SL trigger ~{tick_up(p[l['symbol']]*(1+CFG['sl_pct']))}")
        print(f"  margin available Rs {kite.margins('equity')['net']:,.0f}"); return
    if TODAY.weekday() not in CFG["trade_days"]: log.info("Not a trading day in config."); return
    state = load_state(); state_ref[0] = state
    if "plan" not in state:
        state["plan"] = todays_plan(kite); save_state(state)
    plan = state["plan"]; late = plan["time"] > CFG["entry_time"]
    if state["phase"] == "none":
        if now() > at(CFG["exit_time"]): log.info("Past exit time, nothing to do."); return
        alert(f"Bot started ({plan['label']}): will sell at {plan['time']}" + (" if VIX is at or below its 9:15 level, else sit out" if plan.get("vix_calm") else "") +
              ". Pause any time from the phone page.")
        while now() < at(CFG["entry_time"]):
            time.sleep(1)
        if kill_switch():
            log.info("STOP present - not trading today; tracking shadows only.")
            alert("Paused (STOP) - no live trade today. Shadow strategies are still tracked for learning.")
            state["plan_done"] = True
            try: enter(kite, state, live=False)
            except Exception as e: log.warning(f"shadow-only start failed: {e}"); return
        elif late:                                         # shadows start at 9:20, live entry later
            try: enter(kite, state, live=False, why=f"waiting for {plan['time']}")
            except Exception as e: log.warning(f"shadow start failed: {e}"); state["phase"] = "shadow_only"; save_state(state)
        else:
            state["plan_done"] = True; save_state(state)
            enter(kite, state)
    if state["phase"] == "shadow_only" and not state.get("plan_done"):
        if now() > at(plan["time"]) + dt.timedelta(minutes=30):
            state.update(plan_done=True, exit_reason=f"started too late for {plan['time']} entry - no trade"); save_state(state)
            alert(f"Bot (re)started after {plan['time']}+30 min - no live trade today.")
        elif not wait_with_shadows(at(plan["time"])):
            state.update(plan_done=True, exit_reason="paused (STOP) - shadows only"); save_state(state)
            alert("Paused (STOP) - no live trade today. Shadow strategies are still tracked.")
        else:
            ok, v, o = vix_calm() if plan.get("vix_calm") else (True, None, None)
            state["plan_done"] = True; save_state(state)       # set BEFORE ordering so a restart can never sell twice
            if not ok:
                state["exit_reason"] = f"sat out: VIX {v} above its 9:15 level {o} at {plan['time']}"; save_state(state)
                alert(f"Sitting out today: VIX {v} is above its 9:15 level {o} at {plan['time']}. Shadows still tracked.")
            else:
                if v is not None: log.info(f"VIX calm check passed: {v} <= {o}")
                enter(kite, state)
    elif state["phase"] in ("entering", "exiting"):
        log.warning(f"Restarted mid-{state['phase']} - closing anything open to be safe.")
        od = orders_by_id(kite)
        for l in state["legs"]:
            o = od.get(l.get("sell_oid"), {})
            l["filled"], l["avg"] = o.get("filled_quantity", l.get("filled", 0)), o.get("average_price", l.get("avg", 0))
        exit_all(kite, state, "restart during " + state["phase"])
    if state["phase"] == "entered":
        log.info("Monitoring..."); monitor(kite, state)
    shadow_until_close(state)
    end_of_day(state)

if __name__ == "__main__":
    try: main()
    except Exception as e:
        log.exception(f"FATAL: {e}. Check Kite positions manually; SL orders at the exchange remain active.")
        try: tg(CFG, f"[{MODE}] Bot error: {e}\nCheck Kite positions; SL orders at the exchange stay active.")
        except Exception: pass
        raise
