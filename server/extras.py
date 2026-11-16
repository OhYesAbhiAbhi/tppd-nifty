#!/usr/bin/env python3
"""Extra weekly strategies that run next to the Nifty straddle bot (v14).

  sx_fly  Wednesday  Sensex overnight iron fly   Wed 15:00 -> Thu 09:20  wings +-1000, stop 30% on short legs
  sx_thu  Thursday   Sensex expiry-day straddle  Thu 11:30 -> Thu 15:15  stop 40% per leg + move other leg's stop to cost
  nf_fly  Friday     Nifty weekend iron fly      Fri 15:00 -> Mon 09:20  wings +-500,  stop 30% on short legs
  nf_mon  Monday     Nifty overnight iron fly    Mon 15:13 -> Tue 09:18  wings +-500,  stop 30%
  bn_wed  Wednesday  BankNifty ATM straddle      Wed 09:20 -> Wed 14:55  stop 30% per leg; only the Wednesday
                     before the monthly expiry (expiry 1-7 days away), so about once a month
  sx_wknd Friday     Sensex weekend iron fly     Fri 15:00 -> Mon 09:20  wings +-1000, stop 30% on short legs

Event filter (v9): no overnight fly is opened if a scheduled event (election results, Budget, Fed, RBI, or a date
you add with `extras.py skip`) falls between the entry and the next morning's exit (+3 h). Events marked
intraday also skip the Thursday straddle. Unscheduled news (like the 2 Feb 2026 trade deal) cannot be filtered.

AlgoTest basis (27 Oct 2025 - 26 Oct 2026, 1 lot, net of costs):
  sx_fly +Rs 25.6k (47 trades, 70% win, max DD -8.8k)
  sx_thu +Rs 19.8k (48 trades, 63% win, max DD -6.4k; 10:30 and 12:30 entries made only +0.6k / +4.8k)
  nf_fly +Rs 36.1k (40 trades, 78% win, max DD -7.5k)
  bn_wed +Rs 19.7k (12 trades, 83% win, max DD -3.3k; thin sample. Other Wednesdays lost -18.5k)
  sx_wknd +Rs 28.5k (35 trades, 60% win, max DD -7.2k; same weekend gap risk as nf_fly)
  nf_mon +Rs 11.8k (46 trades, 72% win, max DD -33.8k: one -32k gap night on 3 Feb 2026; +43.9k without it)

Usage (from ~/algo, with the bot's venv):
  venv/bin/python extras.py check [name]        dry run: login, contracts, prices, margin. No orders.
  venv/bin/python extras.py enter <name>         cron: waits for the entry time, enters, monitors
  venv/bin/python extras.py exit  <name>         cron: next-session exit for the flies
  venv/bin/python extras.py status
  venv/bin/python extras.py events               upcoming events and which entries they block
  venv/bin/python extras.py skip YYYY-MM-DD "why"  block every extras entry on that date (adds to events.json)
  venv/bin/python extras.py event YYYY-MM-DD HH:MM "why"  news expected at that IST time: blocks any fly that
                                                 would be held across it, and the Thursday straddle that day

Settings: config.json -> "extras" (see DEFAULTS). Each strategy can be switched off or put on paper on its own.
Stops are watched by polling the LTP (every 2 s); no stop orders rest overnight.

v11 (29 Oct 2026, after the sx_fly double buy):
  - Morning stops on overnight flies ignore the opening ticks: checked once a minute on the 1-minute close,
    from 09:16 (config per strategy: "morning_stop": "close" | "tick" | "off", "morning_stop_start": "09:16").
  - "Couldn't find that order_id" is retried; an order that stays unknown is checked against Kite positions.
  - Every close checks what Kite actually holds first, so a leg is never bought back twice.
  - After an exit, any quantity still showing in Kite for the legs raises a Telegram alert.
  - Entry legs that filled but lost their status are found in Kite and unwound too.
"""
import json, os, socket, sys, time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

try:  # Kite rejects orders sent over this server's IPv6 address: force IPv4 like the main bot.
    import urllib3.util.connection as _uc
    _uc.allowed_gai_family = lambda: socket.AF_INET
except Exception:
    pass

IST = ZoneInfo("Asia/Kolkata")
BASE = os.path.dirname(os.path.abspath(__file__))
CFG_PATH = os.path.join(BASE, "config.json")
JOURNAL = os.path.join(BASE, "extras_journal.csv")
PAUSE_FILES = ["STOP", "PAUSE", "PAUSED", "paused", "pause.flag", "stop.flag"]
TOKEN_FILES = ["access_token.txt", "access_token", ".access_token", "token.txt",
               "session.json", "kite_session.json", "token.json"]

DEFAULTS = {
    "paper": False,
    "limit_buffer_pct": 3.0,
    "sx_fly": {"enabled": True, "paper": None, "lots": 1, "kind": "fly", "index": "SENSEX",
               "weekday": 2, "entry_time": "15:00", "exit_time": "09:20", "wing_points": 1000,
               "stop_pct": 30, "move_to_cost": False, "min_days": 1, "max_days": 3},
    "sx_thu": {"enabled": True, "paper": None, "lots": 1, "kind": "straddle", "index": "SENSEX",
               "weekday": 3, "entry_time": "11:30", "exit_time": "15:15", "stop_pct": 40,
               "move_to_cost": True, "min_days": 0, "max_days": 0},
    "nf_fly": {"enabled": True, "paper": None, "lots": 1, "kind": "fly", "index": "NIFTY",
               "weekday": 4, "entry_time": "15:00", "exit_time": "09:20", "wing_points": 500,
               "stop_pct": 30, "move_to_cost": False, "min_days": 1, "max_days": 5},
    "bn_wed": {"enabled": True, "paper": None, "lots": 1, "kind": "straddle", "index": "BANKNIFTY",
               "weekday": 2, "entry_time": "09:20", "exit_time": "14:55", "stop_pct": 30,
               "move_to_cost": False, "min_days": 1, "max_days": 7},
    "sx_wknd": {"enabled": True, "paper": None, "lots": 1, "kind": "fly", "index": "SENSEX",
                "weekday": 4, "entry_time": "15:00", "exit_time": "09:20", "wing_points": 1000,
                "stop_pct": 30, "move_to_cost": False, "min_days": 5, "max_days": 6},
    "nf_mon": {"enabled": True, "paper": True, "lots": 1, "kind": "fly", "index": "NIFTY",
               "weekday": 0, "entry_time": "15:13", "exit_time": "09:18", "wing_points": 500,
               "stop_pct": 30, "move_to_cost": False, "min_days": 1, "max_days": 1},
}
NAMES = ["sx_fly", "sx_thu", "nf_fly", "nf_mon", "bn_wed", "sx_wknd"]
MARGIN_WAIT_MIN = 7          # live entries wait up to this long for margin (e.g. Monday straddle still closing)
EVENT_DEFAULTS = {"block": ["election", "budget", "fomc", "rbi", "manual", "other"], "after_exit_hours": 3}
EVENTS_FILE = os.path.join(BASE, "events.json")
# Built-in calendar, IST. time = when the news lands. Add more with `extras.py skip` or by editing events.json.
EVENTS = [
    {"date": "2026-11-04", "time": "06:30", "type": "other", "label": "US midterm election results"},
    {"date": "2026-12-04", "time": "10:00", "type": "rbi", "label": "RBI policy", "intraday": True},
    {"date": "2026-12-10", "time": "00:30", "type": "fomc", "label": "Fed decision (9 Dec US)"},
    {"date": "2027-01-28", "time": "00:30", "type": "fomc", "label": "Fed decision (27 Jan US)"},
    {"date": "2027-02-01", "time": "11:00", "type": "budget", "label": "Union Budget", "intraday": True},
    {"date": "2027-02-05", "time": "10:00", "type": "rbi", "label": "RBI policy", "intraday": True},
    {"date": "2027-03-17", "time": "23:30", "type": "fomc", "label": "Fed decision"},
    {"date": "2027-04-28", "time": "23:30", "type": "fomc", "label": "Fed decision"},
    {"date": "2027-06-09", "time": "23:30", "type": "fomc", "label": "Fed decision"},
    {"date": "2027-07-28", "time": "23:30", "type": "fomc", "label": "Fed decision"},
    {"date": "2027-09-15", "time": "23:30", "type": "fomc", "label": "Fed decision"},
    {"date": "2027-10-27", "time": "23:30", "type": "fomc", "label": "Fed decision"},
    {"date": "2027-12-09", "time": "00:30", "type": "fomc", "label": "Fed decision (8 Dec US)"},
    # UP / Punjab / Uttarakhand / Goa / Manipur assembly results: expected Mar-Apr 2027, dates not announced yet.
    # When the Election Commission announces them:  extras.py skip <counting date> "UP election results"
    # (and the day before, for the exit-poll evening).
]
INDEX = {
    "SENSEX": {"exchange": "BFO", "spot": "BSE:SENSEX", "name": "SENSEX"},
    "NIFTY": {"exchange": "NFO", "spot": "NSE:NIFTY 50", "name": "NIFTY"},
    "BANKNIFTY": {"exchange": "NFO", "spot": "NSE:NIFTY BANK", "name": "BANKNIFTY"},
}
DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def now():
    return datetime.now(IST)


_LOGF = {}


def log(name, msg):
    line = f"{now():%Y-%m-%d %H:%M:%S} [{name}] {msg}"
    print(line, flush=True)
    try:
        p = os.path.join(BASE, f"log_extras_{now():%Y%m%d}.txt")
        if p not in _LOGF:
            _LOGF[p] = open(p, "a")
        _LOGF[p].write(line + "\n"); _LOGF[p].flush()
    except Exception:
        pass


def read_cfg():
    with open(CFG_PATH) as f:
        return json.load(f)


def settings(cfg, name):
    user = cfg.get("extras", {}) or {}
    s = dict(DEFAULTS[name]); s.update(user.get(name, {}) or {})
    s["paper"] = bool(user.get("paper", DEFAULTS["paper"]) if s.get("paper") is None else s["paper"])
    s["limit_buffer_pct"] = float(user.get("limit_buffer_pct", DEFAULTS["limit_buffer_pct"]))
    return s


def telegram(cfg, name, text):
    tok = cfg.get("telegram_token") or cfg.get("telegram_bot_token") or cfg.get("tg_token") or cfg.get("bot_token")
    chat = cfg.get("telegram_chat_id") or cfg.get("chat_id") or cfg.get("tg_chat_id")
    if not (tok and chat):
        return
    try:
        import requests
        requests.post(f"https://api.telegram.org/bot{tok}/sendMessage",
                      data={"chat_id": chat, "text": f"[{name}] {text}"}, timeout=10)
    except Exception as e:
        log(name, f"telegram failed: {e}")


def find_token(cfg):
    for k in ("access_token", "kite_access_token", "token"):
        if cfg.get(k):
            return cfg[k], f"config.json:{k}"
    for fn in TOKEN_FILES:
        p = os.path.join(BASE, fn)
        if os.path.exists(p):
            raw = open(p).read().strip()
            if raw.startswith("{"):
                try:
                    j = json.loads(raw); t = j.get("access_token") or j.get("token")
                    if t:
                        return t, fn
                except Exception:
                    pass
            elif raw:
                return raw.split()[0], fn
    return None, None


def kite_login(cfg, name):
    from kiteconnect import KiteConnect
    api_key = cfg.get("api_key") or cfg.get("kite_api_key")
    token, src = find_token(cfg)
    if not api_key or not token:
        raise SystemExit("Could not find api_key / access_token in config.json or " + ", ".join(TOKEN_FILES))
    k = KiteConnect(api_key=api_key); k.set_access_token(token)
    log(name, f"login: token from {src}")
    return k


def login_retry(name, until_hhmm, why):
    h, m = map(int, until_hhmm.split(":"))
    end = now().replace(hour=h, minute=m, second=0, microsecond=0)
    last = None
    while True:
        cfg = read_cfg()
        try:
            k = kite_login(cfg, name); k.profile(); return k
        except SystemExit:
            raise
        except Exception as e:
            if now() >= end:
                raise RuntimeError(f"login still failing at {until_hhmm}: {e}")
            if last is None or (now() - last).total_seconds() > 120:
                log(name, f"login not ready ({e}) - retrying")
                telegram(cfg, name, f"⚠️ {why}: Zerodha login needed now (phone page → Log in)")
                last = now()
            time.sleep(20)


def paused():
    return [p for p in PAUSE_FILES if os.path.exists(os.path.join(BASE, p))]


def tick(x):
    return max(0.05, round(round(x / 0.05) * 0.05, 2))


def at(hhmm, sec=0, day=None):
    h, m = map(int, hhmm.split(":"))
    d = day or now()
    return d.replace(hour=h, minute=m, second=sec, microsecond=0)


def wait_until(t):
    while now() < t:
        time.sleep(min(5, max(0.2, (t - now()).total_seconds())))


def state_path(name):
    return os.path.join(BASE, f"state_{name}.json")


def save_state(name, st):
    with open(state_path(name), "w") as f:
        json.dump(st, f, indent=1, default=str)


def load_state(name):
    p = state_path(name)
    return json.load(open(p)) if os.path.exists(p) else None


# ------------------------------------------------------------------ market data / orders
def pick_contracts(kite, s):
    ix = INDEX[s["index"]]
    spot = kite.ltp([ix["spot"]])[ix["spot"]]["last_price"]
    inst = [i for i in kite.instruments(ix["exchange"])
            if i.get("name") == ix["name"] and i.get("instrument_type") in ("CE", "PE")]
    today = now().date()
    exps = sorted({i["expiry"] for i in inst if i["expiry"] >= today})
    if not exps:
        raise RuntimeError("no option expiries found")
    exp = exps[0]
    strikes = sorted({i["strike"] for i in inst if i["expiry"] == exp})
    atm = min(strikes, key=lambda x: abs(x - spot))

    def get(strike, typ):
        return next((i for i in inst if i["expiry"] == exp and i["strike"] == strike and i["instrument_type"] == typ), None)
    legs = {"short_ce": get(atm, "CE"), "short_pe": get(atm, "PE")}
    if s["kind"] == "fly":
        w = s["wing_points"]
        legs["long_ce"] = get(min(strikes, key=lambda x: abs(x - (atm + w))), "CE")
        legs["long_pe"] = get(min(strikes, key=lambda x: abs(x - (atm - w))), "PE")
    missing = [k for k, v in legs.items() if v is None]
    if missing:
        raise RuntimeError(f"missing contracts: {missing}")
    return ix, spot, exp, atm, legs


def ltps(kite, exch, syms):
    q = kite.ltp([f"{exch}:{x}" for x in syms])
    return {x: q[f"{exch}:{x}"]["last_price"] for x in syms}


class OrderUnknown(RuntimeError):
    def __init__(self, oid, msg):
        super().__init__(msg); self.oid = oid


def order_last(kite, oid, tries=8):
    """Latest status row of an order. Kite can answer "Couldn't find that order_id" for a moment after placing."""
    err = None
    for _ in range(tries):
        try:
            h = kite.order_history(oid)
            if h:
                return h[-1]
        except Exception as e:
            err = e
        time.sleep(0.4)
    try:
        for o in kite.orders():
            if str(o.get("order_id")) == str(oid):
                return o
    except Exception as e:
        err = e
    raise OrderUnknown(oid, f"order {oid} status unknown: {err}")


def net_qty(kite, exch, sym):
    """Net open NRML quantity Kite shows for this symbol (negative = short), or None if it can't be read."""
    for _ in range(3):
        try:
            return sum(int(p.get("quantity") or 0) for p in kite.positions()["net"]
                       if p.get("tradingsymbol") == sym and p.get("exchange") == exch and p.get("product") == "NRML")
        except Exception:
            time.sleep(1)
    return None


def last_fill(kite, sym, side):
    """Average price of today's latest COMPLETE order in sym on that side, or None."""
    try:
        done = [o for o in kite.orders() if o.get("tradingsymbol") == sym and o.get("transaction_type") == side
                and o.get("status") == "COMPLETE"]
    except Exception:
        return None
    if not done:
        return None
    o = max(done, key=lambda o: str(o.get("order_timestamp") or o.get("exchange_timestamp") or ""))
    return float(o.get("average_price") or 0) or None


def leg_is_flat(held, leg_side):
    """True when Kite no longer holds the leg: zero, or the opposite sign (it was closed more than once)."""
    if held is None:
        return False
    return held == 0 or (held > 0) == (leg_side == "SELL")


def execute(kite, name, s, exch, sym, side, qty, ref, paper):
    buf = s["limit_buffer_pct"] / 100.0
    if paper:
        log(name, f"PAPER {side} {qty} {sym} @ {ref:.2f}")
        return float(ref)
    price = tick(ref * (1 + buf) if side == "BUY" else ref * (1 - buf))
    oid = kite.place_order(variety="regular", exchange=exch, tradingsymbol=sym, transaction_type=side,
                           quantity=qty, product="NRML", order_type="LIMIT", price=price,
                           validity="DAY", tag=name[:20])
    log(name, f"order {oid}: {side} {qty} {sym} LIMIT {price}")
    for attempt in range(5):
        for _ in range(10):
            time.sleep(0.5)
            h = order_last(kite, oid)
            if h.get("status") == "COMPLETE":
                avg = float(h.get("average_price") or price)
                log(name, f"filled {side} {sym} @ {avg}")
                return avg
            if h.get("status") in ("REJECTED", "CANCELLED"):
                raise RuntimeError(f"order {oid} {h.get('status')}: {h.get('status_message')}")
        lp = ltps(kite, exch, [sym])[sym]
        b2 = buf * (attempt + 2)
        price = tick(lp * (1 + b2) if side == "BUY" else max(0.05, lp * (1 - b2)))
        try:
            kite.modify_order(variety="regular", order_id=oid, price=price); log(name, f"chase {oid} -> {price}")
        except Exception as e:
            log(name, f"modify failed: {e}")
    try:                                              # don't leave a live order behind that could fill later
        kite.cancel_order(variety="regular", order_id=oid); log(name, f"cancelled unfilled {oid}")
        h = order_last(kite, oid)
        if h.get("status") == "COMPLETE":
            avg = float(h.get("average_price") or price); log(name, f"filled {side} {sym} @ {avg} (at cancel)")
            return avg
    except OrderUnknown:
        raise
    except Exception as e:
        log(name, f"cancel failed: {e}")
    raise RuntimeError(f"order {oid} for {sym} not filled after chasing")


def close_leg(kite, cfg, name, s, st, k, reason):
    L = st["legs"][k]
    exch, sym = st["exchange"], L["symbol"]
    side = "BUY" if L["side"] == "SELL" else "SELL"
    qty = st["qty"]
    if not st["paper"]:                               # what does Kite actually hold?
        held = net_qty(kite, exch, sym)
        if leg_is_flat(held, L["side"]):
            fill = last_fill(kite, sym, side) or ltps(kite, exch, [sym])[sym]
            log(name, f"{sym}: Kite shows qty {held} - already closed, recording exit @ {fill} (no new order)")
            telegram(cfg, name, f"{sym} was already closed in Kite (qty {held}); recorded @ {fill}, no new order")
            L.update(open=False, exit=fill, exit_time=str(now()), reason=reason + "/reconciled")
            save_state(name, st)
            return fill
        if held is not None:
            qty = min(abs(held), st["qty"])
    p = ltps(kite, exch, [sym])[sym]
    try:
        fill = execute(kite, name, s, exch, sym, side, qty, p, st["paper"])
    except Exception as e:
        if st["paper"]:
            raise
        time.sleep(1)
        held = net_qty(kite, exch, sym)
        if not leg_is_flat(held, L["side"]):
            raise
        fill = last_fill(kite, sym, side) or p
        log(name, f"order error ({e}) but Kite shows {sym} qty {held} - closed @ {fill}")
        reason += "/reconciled"
    L.update(open=False, exit=fill, exit_time=str(now()), reason=reason)
    save_state(name, st)
    return fill


def next_minute(sec=1):
    t = now().replace(second=sec, microsecond=0) + timedelta(minutes=1)
    return t


def book_price(kite, exch, sym):
    """Fair buy-back price now: middle of the best bid and ask; the LTP if one side of the book is empty."""
    key = f"{exch}:{sym}"
    q = kite.quote([key])[key]
    d = q.get("depth") or {}
    bid = next((x["price"] for x in d.get("buy", []) if x.get("price")), 0)
    ask = next((x["price"] for x in d.get("sell", []) if x.get("price")), 0)
    if bid and ask:
        return (bid + ask) / 2.0, f"bid {bid} ask {ask}"
    return float(q["last_price"]), f"one-sided book, ltp {q['last_price']}"


def confirm_stop(kite, exch, sym, level, s):
    """Opening window: stop only if two book reads a moment apart are both at/above the stop.
    If the book can't be read, the stop fires: an unreadable quote must never hold a stop back."""
    seen = []
    for i in range(2):
        if i:
            time.sleep(float(s.get("confirm_gap_sec", 2)))
        try:
            px, info = book_price(kite, exch, sym)
        except Exception as e:
            return True, f"quote failed ({e})"
        seen.append(f"{px:.2f} ({info})")
        if px < level:
            return False, "; ".join(seen)
    return True, "; ".join(seen)


def opening_gap(kite, s):
    """Index gap % from yesterday's close to today's open; None if it can't be read."""
    key = INDEX[s["index"]]["spot"]
    try:
        q = kite.quote([key])[key]
        o = q.get("ohlc") or {}
        op = float(o.get("open") or q.get("last_price") or 0)
        cl = float(o.get("close") or 0)
        return round((op / cl - 1) * 100, 3) if op and cl else None
    except Exception:
        return None


def gap_against(gap, k):
    """Gap % against a short leg: gap up hurts the short call, gap down hurts the short put."""
    return None if gap is None else (gap if k == "short_ce" else -gap)


def watch(kite, cfg, name, s, st, until, on_close=False, confirm_until=None, gap=None):
    """Stop each short leg at entry*(1+stop); with move_to_cost, after the first stop the other short's stop = its entry.
    on_close=True: check once a minute, just after the minute turns (the 1-minute close), not every tick."""
    mult = 1 + s["stop_pct"] / 100.0
    first = True
    while now() < until:
        if on_close and not first:
            wait_until(min(next_minute(), until))
            if now() >= until:
                return
        first = False
        shorts = [k for k in ("short_ce", "short_pe") if st["legs"][k]["open"]]
        if not shorts:
            return
        try:
            px = ltps(kite, st["exchange"], [st["legs"][k]["symbol"] for k in shorts])
        except Exception as e:
            log(name, f"ltp error: {e}"); time.sleep(3); continue
        for k in shorts:
            L = st["legs"][k]
            level = L.get("stop") or L["entry"] * mult
            p = px[L["symbol"]]
            if p >= level:
                if confirm_until and now() < confirm_until:
                    against = gap_against(gap, k)
                    if against is not None and against >= float(s.get("gap_fast_pct", 0.15)):
                        log(name, f"opening stop {L['symbol']}: gap {against}% against the leg, no book check")
                    else:
                        ok, info = confirm_stop(kite, st["exchange"], L["symbol"], level, s)
                        log(name, f"opening check {L['symbol']}: ltp {p} >= {level:.2f}; gap against {against}; book {info} -> "
                                  f"{'real, stopping' if ok else 'bad print, ignored'}")
                        if not ok:
                            if not L.get("print_alerted"):
                                L["print_alerted"] = True; save_state(name, st)
                                telegram(cfg, name, f"bad opening print ignored on {L['symbol']}: ltp {p}, stop {level:.2f}, book {info}")
                            continue
                why = "cost" if L.get("stop") else "stop"
                log(name, f"{why.upper()} {k} {L['symbol']}: ltp {p} >= {level:.2f}")
                try:
                    fill = close_leg(kite, cfg, name, s, st, k, why)
                except Exception as e:
                    log(name, f"STOP ORDER FAILED {L['symbol']}: {e}")
                    if not L.get("alerted"):
                        L["alerted"] = True; save_state(name, st)
                        telegram(cfg, name, f"⚠️ stop order failed for {L['symbol']} and Kite still shows it open "
                                            f"- check Positions ({e}). The bot will retry.")
                    time.sleep(5); continue
                telegram(cfg, name, f"{why} hit on {L['symbol']}: sold {L['entry']}, bought back {fill}")
                if s["move_to_cost"]:
                    for o in ("short_ce", "short_pe"):
                        if o != k and st["legs"][o]["open"] and not st["legs"][o].get("stop"):
                            st["legs"][o]["stop"] = st["legs"][o]["entry"]
                            log(name, f"move to cost: {o} stop -> {st['legs'][o]['entry']}")
                    save_state(name, st)
        if not on_close:
            time.sleep(2)


def shorts_first(st, keys):
    """Close short legs before wings: selling a wing while its short is open needs naked-short margin (2 Nov)."""
    return sorted(keys, key=lambda k: 0 if st["legs"][k]["side"] == "SELL" else 1)


def finish(kite, cfg, name, s, st):
    failed = []
    for k in shorts_first(st, list(st["legs"])):
        L = st["legs"][k]
        if L["open"]:
            try:
                close_leg(kite, cfg, name, s, st, k, "time")
            except Exception as e:
                log(name, f"exit attempt failed {L['symbol']}: {e} - will retry"); failed.append(k)
    for k in failed:                                  # retry once, after everything else is closed
        L = st["legs"][k]
        if L["open"]:
            try:
                time.sleep(1); close_leg(kite, cfg, name, s, st, k, "time")
            except Exception as e:
                log(name, f"EXIT FAILED {L['symbol']}: {e}")
                telegram(cfg, name, f"⚠️ exit failed for {L['symbol']} - close it manually ({e})")
    if not st["paper"]:                               # does Kite agree that everything is flat?
        time.sleep(2)
        left = []
        for L in st["legs"].values():
            held = net_qty(kite, st["exchange"], L["symbol"])
            if held:
                left.append(f"{L['symbol']} qty {held}")
        if left:
            log(name, f"LEFTOVER in Kite after exit: {left}")
            telegram(cfg, name, "⚠️ Kite still shows " + ", ".join(left) + " after the exit - close it manually")
    pnl = sum(((L["entry"] - L["exit"]) if L["side"] == "SELL" else (L["exit"] - L["entry"])) * st["qty"]
              for L in st["legs"].values() if "exit" in L)
    st["status"] = "closed" if all(not L["open"] for L in st["legs"].values()) else "partly_open"
    st["gross_pnl"] = round(pnl, 2); save_state(name, st)
    new = not os.path.exists(JOURNAL)
    with open(JOURNAL, "a") as f:
        if new:
            f.write("strategy,entry_date,exit_date,expiry,atm,qty,paper,gross_pnl,legs\n")
        f.write(f"{name},{st['date']},{now().date()},{st['expiry']},{st['atm']},{st['qty']},{st['paper']},{pnl:.2f},"
                + "|".join(f"{k}:{v['entry']}->{v.get('exit')}:{v.get('reason')}" for k, v in st["legs"].items()) + "\n")
    msg = f"{'PAPER ' if st['paper'] else ''}closed ({st['status']}). gross P&L Rs {pnl:,.0f} before charges"
    log(name, msg); telegram(cfg, name, msg)


# ------------------------------------------------------------------ events
def load_events():
    ev = [dict(e) for e in EVENTS]
    if os.path.exists(EVENTS_FILE):
        try:
            j = json.load(open(EVENTS_FILE))
            ev += j.get("events", []) if isinstance(j, dict) else j
        except Exception as e:
            log("events", f"events.json unreadable ({e}) - using built-in list only")
    for e in ev:
        if e.get("skip"):
            e.setdefault("type", "manual"); e.setdefault("time", "00:00")
        e.setdefault("type", "other"); e.setdefault("time", "09:15")
        e["dt"] = datetime.strptime(f"{e['date']} {e['time']}", "%Y-%m-%d %H:%M").replace(tzinfo=IST)
    return sorted(ev, key=lambda e: e["dt"])


def next_weekday(d):
    d = d + timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def blocking_events(cfg, s, day=None):
    """Events that rule out this strategy's entry on `day` (default today)."""
    ec = dict(EVENT_DEFAULTS); ec.update(cfg.get("extras", {}).get("events", {}) or {})
    block = set(ec["block"]) | {"manual"}
    day = day or now()
    entry = at(s["entry_time"], day=day)
    if s["kind"] == "fly":
        end = at(s["exit_time"], day=next_weekday(day)) + timedelta(hours=float(ec["after_exit_hours"]))
    else:
        end = at(s["exit_time"], day=day)
    out = []
    for e in load_events():
        if e["type"] not in block:
            continue
        if e.get("skip") and e["date"] == str(day.date()):
            out.append(e); continue
        if s["kind"] == "fly" and entry < e["dt"] <= end:
            out.append(e)
        elif s["kind"] == "straddle" and e.get("intraday") and e["date"] == str(day.date()):
            out.append(e)
    return out


def event_skip(cfg, name, s):
    ev = blocking_events(cfg, s)
    if ev:
        why = "; ".join(f"{e['label']} ({e['date']} {e['time']})" for e in ev)
        log(name, f"event filter - skip: {why}"); telegram(cfg, name, f"skip today (event): {why}")
        return True
    return False


def margin_gate(kite, cfg, name, s, ix, legs, qty, entry):
    """Live only: wait (up to MARGIN_WAIT_MIN) until free margin covers the basket. Returns True to go ahead."""
    basket = [dict(exchange=ix["exchange"], tradingsymbol=v["tradingsymbol"],
                   transaction_type="SELL" if k.startswith("short") else "BUY", variety="regular",
                   product="NRML", order_type="MARKET", quantity=qty, price=0) for k, v in legs.items()]
    deadline = entry + timedelta(minutes=MARGIN_WAIT_MIN); told = False
    while True:
        try:
            need = kite.basket_order_margins(basket, consider_positions=True)["final"]["total"]
            m = kite.margins("equity"); avail = m.get("net") or m["available"]["live_balance"]
        except Exception as e:
            log(name, f"margin check failed ({e}) - placing orders anyway"); return True
        if avail >= need * 1.03:
            log(name, f"margin ok: need ~Rs {need:,.0f}, free Rs {avail:,.0f}"); return True
        if now() >= deadline:
            log(name, f"margin short: need ~Rs {need:,.0f}, free Rs {avail:,.0f} - skip")
            telegram(cfg, name, f"skip: margin short (need ~Rs {need:,.0f}, free Rs {avail:,.0f})"); return False
        if not told:
            log(name, f"waiting for margin: need ~Rs {need:,.0f}, free Rs {avail:,.0f}"); told = True
        time.sleep(10)


# ------------------------------------------------------------------ commands
def cmd_enter(name):
    cfg = read_cfg(); s = settings(cfg, name)
    if not s["enabled"]:
        log(name, "disabled in config - skip"); return
    if now().weekday() != s["weekday"]:
        log(name, f"today is not {DAYS[s['weekday']]} (IST) - skip"); return
    old = load_state(name)
    if old and old.get("status") in ("open", "opening"):
        log(name, "a position is already open - skip"); telegram(cfg, name, "skip: previous position still open"); return
    if paused():
        log(name, f"paused ({paused()}) - skip"); telegram(cfg, name, "paused - no entry"); return
    if event_skip(cfg, name, s):
        return
    entry = at(s["entry_time"])
    wait_until(entry - timedelta(minutes=2))
    kite = login_retry(name, s["entry_time"], f"entry at {s['entry_time']}")
    wait_until(entry)
    cfg = read_cfg()
    if paused():
        log(name, "paused at entry time - skip"); return
    if event_skip(cfg, name, s):                      # picks up a `skip` added during the day
        return
    ix, spot, exp, atm, legs = pick_contracts(kite, s)
    days = (exp - now().date()).days
    if not (s["min_days"] <= days <= s["max_days"]):
        log(name, f"expiry {exp} is {days} day(s) away, outside {s['min_days']}-{s['max_days']} - skip")
        telegram(cfg, name, f"skip: expiry {exp} ({days} days away)"); return
    qty = int(legs["short_ce"]["lot_size"]) * int(s["lots"])
    paper = s["paper"]
    if not paper and not margin_gate(kite, cfg, name, s, ix, legs, qty, entry):
        return
    if not paper:                                     # prices may have moved while waiting for margin
        ix, spot, exp, atm, legs = pick_contracts(kite, s)
    st = {"status": "opening", "strategy": name, "date": str(now().date()), "expiry": str(exp), "spot": spot,
          "atm": atm, "qty": qty, "paper": paper, "exchange": ix["exchange"], "legs": {}}
    save_state(name, st)
    px = ltps(kite, ix["exchange"], [v["tradingsymbol"] for v in legs.values()])
    order = [(k, "BUY") for k in ("long_ce", "long_pe") if k in legs] + [("short_ce", "SELL"), ("short_pe", "SELL")]
    done = []
    try:
        for k, side in order:
            sym = legs[k]["tradingsymbol"]
            fill = execute(kite, name, s, ix["exchange"], sym, side, qty, px[sym], paper)
            st["legs"][k] = {"symbol": sym, "side": side, "entry": fill, "open": True}
            done.append(k); save_state(name, st)
    except Exception as e:
        if not paper and k not in done:               # did the failed order fill anyway?
            time.sleep(1)
            held = net_qty(kite, ix["exchange"], sym)
            if held and (held < 0) == (side == "SELL"):
                f2 = last_fill(kite, sym, side) or px[sym]
                st["legs"][k] = {"symbol": sym, "side": side, "entry": f2, "open": True}
                done.append(k); save_state(name, st)
                log(name, f"{sym} filled despite the error ({held}) @ {f2} - included in unwind")
        log(name, f"ENTRY FAILED: {e} - unwinding {done}")
        telegram(cfg, name, f"entry failed ({e}); unwinding {done}")
        for k in shorts_first(st, done):
            try:
                close_leg(kite, cfg, name, s, st, k, "unwind")
            except Exception as e2:
                log(name, f"UNWIND FAILED {st['legs'][k]['symbol']}: {e2}")
                telegram(cfg, name, f"⚠️ could not unwind {st['legs'][k]['symbol']} - close it manually")
        st["status"] = "failed"; save_state(name, st); return
    st["status"] = "open"; save_state(name, st)
    credit = sum(v["entry"] if v["side"] == "SELL" else -v["entry"] for v in st["legs"].values())
    msg = (f"{'PAPER ' if paper else ''}entered | {s['index']} {spot:.0f}, ATM {atm}, expiry {exp}\n"
           + "\n".join(f"{k}: {v['side']} {v['symbol']} @ {v['entry']}" for k, v in st["legs"].items())
           + f"\nnet credit {credit:.1f} pts (Rs {credit*qty:,.0f}); stop +{s['stop_pct']}%"
           + (" + move to cost" if s["move_to_cost"] else ""))
    log(name, msg); telegram(cfg, name, msg)
    if s["kind"] == "straddle":                       # same-day trade: watch to exit time, then close
        watch(kite, cfg, name, s, st, at(s["exit_time"]))
        wait_until(at(s["exit_time"]))
        finish(kite, cfg, name, s, st)
    else:                                             # overnight fly: watch until the close, hold
        watch(kite, cfg, name, s, st, at("15:29"))
        log(name, "position held overnight")


def cmd_exit(name):
    cfg = read_cfg(); s = settings(cfg, name)
    st = load_state(name)
    if not st or st.get("status") != "open":
        log(name, "no open position - nothing to do"); return
    if str(now().date()) == st["date"]:
        log(name, "exit called on the entry day - nothing to do"); return
    wait_until(at("09:05"))
    kite = login_retry(name, "11:00", f"open {name} position must be closed at {s['exit_time']}")
    cfg = read_cfg()
    mode = s.get("morning_stop", "close")
    if mode == "off":
        log(name, "morning stop off - holding to the exit time")
    elif mode == "confirm":                           # every tick; small-gap 09:15 stops need a real order book
        wait_until(at("09:15", 2))
        gap = opening_gap(kite, s)
        log(name, f"morning stop: {s['index']} gap {gap}%, book check until " + s.get("confirm_until", "09:16"))
        watch(kite, cfg, name, s, st, at(s["exit_time"]), confirm_until=at(s.get("confirm_until", "09:16")), gap=gap)
    elif mode == "tick":
        wait_until(at("09:15", 2))
        watch(kite, cfg, name, s, st, at(s["exit_time"]))
    else:                                             # "close": 1-minute closes from 09:16, skips the opening ticks
        wait_until(at(s.get("morning_stop_start", "09:16"), 1))
        log(name, "morning stop: checking 1-minute closes")
        watch(kite, cfg, name, s, st, at(s["exit_time"]), on_close=True)
    wait_until(at(s["exit_time"]))
    finish(kite, cfg, name, s, st)


def cmd_check(only=None):
    cfg = read_cfg()
    kite = kite_login(cfg, "check")
    p = kite.profile(); log("check", f"logged in as {p.get('user_name')} ({p.get('user_id')})")
    for name in ([only] if only else NAMES):
        s = settings(cfg, name)
        try:
            ix, spot, exp, atm, legs = pick_contracts(kite, s)
        except Exception as e:
            log(name, f"contract lookup failed: {e}"); continue
        qty = int(legs["short_ce"]["lot_size"]) * int(s["lots"])
        days = (exp - now().date()).days
        px = ltps(kite, ix["exchange"], [v["tradingsymbol"] for v in legs.values()])
        log(name, f"{DAYS[s['weekday']]} {s['entry_time']} | {s['index']} {spot:.1f} | expiry {exp} ({days}d; "
                  f"trades when {s['min_days']}-{s['max_days']}d) | ATM {atm} | qty {qty} | "
                  f"{'PAPER' if s['paper'] else 'LIVE'} | enabled={s['enabled']}")
        for k, v in legs.items():
            log(name, f"   {k:9s} {v['tradingsymbol']:24s} LTP {px[v['tradingsymbol']]}")
        try:
            basket = [dict(exchange=ix["exchange"], tradingsymbol=v["tradingsymbol"],
                           transaction_type="SELL" if k.startswith("short") else "BUY", variety="regular",
                           product="NRML", order_type="MARKET", quantity=qty, price=0) for k, v in legs.items()]
            m = kite.basket_order_margins(basket, consider_positions=True)
            need = m.get("final", {}).get("total") if isinstance(m, dict) else None
            log(name, f"   margin needed ~Rs {need:,.0f}" if need else f"   margin: {str(m)[:150]}")
        except Exception as e:
            log(name, f"   margin check skipped: {e}")
    try:
        log("check", f"available margin Rs {kite.margins('equity')['available']['live_balance']:,.0f}")
    except Exception:
        pass
    log("check", f"pause files: {paused() or 'none'} - no orders placed")
    cmd_events(days=21)


def cmd_events(days=60):
    cfg = read_cfg(); t0 = now(); upto = t0 + timedelta(days=days)
    up = [e for e in load_events() if t0 - timedelta(days=1) <= e["dt"] <= upto]
    print(f"events in the next {days} days: {len(up)}")
    for e in up:
        print(f"  {e['date']} {e['time']}  {e['type']:8s} {e['label']}")
    d = t0
    while d <= upto:                                   # which entries are blocked
        for name in NAMES:
            s = settings(cfg, name)
            if s["enabled"] and d.weekday() == s["weekday"]:
                ev = blocking_events(cfg, s, day=d)
                if ev:
                    print(f"  -> {d:%a %d %b} {name} SKIPPED: " + "; ".join(e["label"] for e in ev))
        d += timedelta(days=1)


def cmd_skip(day, label):
    datetime.strptime(day, "%Y-%m-%d")
    j = json.load(open(EVENTS_FILE)) if os.path.exists(EVENTS_FILE) else {"events": []}
    j.setdefault("events", []).append({"date": day, "label": label or "manual skip", "skip": True})
    json.dump(j, open(EVENTS_FILE, "w"), indent=1)
    print(f"added: no extras entries on {day} ({label})")


def cmd_event(day, hhmm, label):
    datetime.strptime(f"{day} {hhmm}", "%Y-%m-%d %H:%M")
    j = json.load(open(EVENTS_FILE)) if os.path.exists(EVENTS_FILE) else {"events": []}
    j.setdefault("events", []).append({"date": day, "time": hhmm, "type": "manual",
                                       "label": label or "manual event", "intraday": True})
    json.dump(j, open(EVENTS_FILE, "w"), indent=1)
    print(f"added event {day} {hhmm} ({label}) - affected entries:")
    cmd_events(days=max(7, (datetime.strptime(day, "%Y-%m-%d").date() - now().date()).days + 4))


def cmd_status():
    for name in NAMES:
        st = load_state(name)
        print(name, json.dumps(st, indent=1) if st else "no state")


if __name__ == "__main__":
    a = sys.argv[1:] or ["check"]
    if a[0] == "check":
        cmd_check(a[1] if len(a) > 1 else None)
    elif a[0] == "status":
        cmd_status()
    elif a[0] == "events":
        cmd_events(int(a[1]) if len(a) > 1 else 60)
    elif a[0] == "event" and len(a) > 2:
        cmd_event(a[1], a[2], " ".join(a[3:]))
    elif a[0] == "skip" and len(a) > 1:
        cmd_skip(a[1], " ".join(a[2:]))
    elif a[0] in ("enter", "exit") and len(a) > 1 and a[1] in DEFAULTS and a[1] not in ("paper", "limit_buffer_pct"):
        (cmd_enter if a[0] == "enter" else cmd_exit)(a[1])
    else:
        print(__doc__)
