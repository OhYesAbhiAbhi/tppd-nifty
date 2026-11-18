"""Offline tests for btst.py (no Kite, no keys). Run: python -m unittest test_btst -v"""
import json
import os
import tempfile
import unittest
from datetime import date, datetime

import extras as X
import btst as B

IST = X.IST


def T(d, hhmm, sec=0):
    h, m = map(int, hhmm.split(":"))
    return datetime(d.year, d.month, d.day, h, m, sec, tzinfo=IST)


class FakeKite:
    """Fills every order at the current price and tracks NRML positions."""

    def __init__(self):
        self.px, self.pos, self.orders_, self.free, self.n = {}, {}, [], 10_000_000, 0
        self.leg_margin = {"BFO": 150_000, "NFO": 120_000}
        self.reject = set()

    def instruments(self, exch):
        out = []
        if exch == "BFO":
            for e in (date(2026, 11, 26), date(2026, 12, 3)):
                for k in range(80000, 84100, 100):
                    for typ in ("CE", "PE"):
                        out.append(dict(name="SENSEX", instrument_type=typ, expiry=e, strike=float(k), lot_size=20,
                                        tradingsymbol=f"SX{e:%m%d}{k}{typ}"))
        else:
            for e in (date(2026, 11, 24), date(2026, 12, 1)):
                for k in range(24500, 25550, 50):
                    for typ in ("CE", "PE"):
                        out.append(dict(name="NIFTY", instrument_type=typ, expiry=e, strike=float(k), lot_size=65,
                                        tradingsymbol=f"NF{e:%m%d}{k}{typ}"))
        return out

    def ltp(self, keys):
        return {k: {"last_price": self.px[k]} for k in keys}

    def quote(self, keys):
        return {k: {"last_price": self.px.get(k, 0), "timestamp": datetime.now()} for k in keys}

    def place_order(self, **o):
        key = f"{o['exchange']}:{o['tradingsymbol']}"
        if o["tradingsymbol"] in self.reject:
            raise RuntimeError("RMS: margin exceeds")
        self.n += 1
        fill = self.px[key]
        q = o["quantity"] * (1 if o["transaction_type"] == "BUY" else -1)
        self.pos[(o["exchange"], o["tradingsymbol"])] = self.pos.get((o["exchange"], o["tradingsymbol"]), 0) + q
        self.orders_.append(dict(order_id=str(self.n), tradingsymbol=o["tradingsymbol"], status="COMPLETE",
                                 transaction_type=o["transaction_type"], average_price=fill,
                                 order_timestamp=f"{self.n:06d}"))
        return str(self.n)

    def order_history(self, oid):
        return [o for o in self.orders_ if o["order_id"] == oid]

    def orders(self):
        return self.orders_

    def positions(self):
        return {"net": [dict(tradingsymbol=s, exchange=e, product="NRML", quantity=q) for (e, s), q in self.pos.items()]}

    def margins(self, seg):
        return {"net": self.free}

    def basket_order_margins(self, b, consider_positions=True):
        return {"final": {"total": sum(self.leg_margin[o["exchange"]] for o in b)}}

    def modify_order(self, **k):
        pass

    def cancel_order(self, **k):
        pass


class BtstTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._saved = (X.BASE, B.DIR, X.telegram, X.log, X.paused, X.load_events)
        X.BASE = B.DIR = self.tmp
        self.sent = []
        X.telegram = lambda cfg, name, text: self.sent.append(f"[{name}] {text}")
        X.log = lambda name, msg: None
        X.paused = lambda: []
        X.load_events = lambda: []
        self.cfg = {}
        self.s = B.settings(self.cfg)
        self.k = FakeKite()

    def tearDown(self):
        X.BASE, B.DIR, X.telegram, X.log, X.paused, X.load_events = self._saved

    def set_px(self, **kw):
        for k, v in kw.items():
            self.k.px[k] = v

    def tick(self, runs, t, warned=None):
        B.loop_once(self.k, self.cfg, self.s, runs, t, warned if warned is not None else set())

    def test_settings_merge(self):
        s = B.settings({"btst": {"lots": {"NIFTY": 2}, "momentum_pct": 4}})
        self.assertEqual(s["lots"], {"SENSEX": 1, "NIFTY": 2})
        self.assertEqual(s["momentum_pct"], 4)
        self.assertEqual(s["days"]["1"], ["SENSEX", "NIFTY"])
        self.assertEqual(s["days"]["3"], ["SENSEX"])

    def test_pick_strikes_and_expiry(self):
        tue = date(2026, 11, 24)                     # Nifty expiry day -> next week; Sensex -> its Thursday
        self.set_px(**{"BSE:SENSEX": 82040.0, "NSE:NIFTY 50": 25012.0})
        sx = B.pick(self.k, self.s, "SENSEX", T(tue, "09:45"))
        nf = B.pick(self.k, self.s, "NIFTY", T(tue, "09:45"))
        self.assertEqual((sx["expiry"], sx["legs"]["ce"]["symbol"], sx["legs"]["pe"]["symbol"], sx["qty"]),
                         ("2026-11-26", "SX112682300CE", "SX112681700PE", 20))
        self.assertEqual((nf["expiry"], nf["legs"]["ce"]["symbol"], nf["legs"]["pe"]["symbol"], nf["qty"]),
                         ("2026-12-01", "NF120125150CE", "NF120124850PE", 65))

    def test_full_tuesday_and_wednesday_exit(self):
        tue, wed = date(2026, 11, 24), date(2026, 11, 25)
        SCE, SPE, NCE, NPE = ("BFO:SX112682300CE", "BFO:SX112681700PE", "NFO:NF120125150CE", "NFO:NF120124850PE")
        self.set_px(**{"BSE:SENSEX": 82000.0, "NSE:NIFTY 50": 25000.0, SCE: 100, SPE: 90, NCE: 60, NPE: 50})
        runs = []
        self.tick(runs, T(tue, "09:44"))
        self.assertEqual(runs, [])                    # nothing before 09:45
        self.tick(runs, T(tue, "09:45"))
        self.assertEqual([r["index"] for r in runs], ["SENSEX", "NIFTY"])
        self.set_px(**{SCE: 95, SPE: 85, NCE: 55, NPE: 48})
        self.tick(runs, T(tue, "10:30"))
        self.set_px(**{SCE: 97, SPE: 88, NCE: 58, NPE: 49})
        self.tick(runs, T(tue, "12:44"))
        sx, nf = runs
        self.assertEqual((sx["legs"]["ce"]["low"], sx["legs"]["pe"]["low"]), (95, 85))
        # 12:46: range closed, armed; prices not 5% below the low yet
        self.set_px(**{SCE: 92, SPE: 82, NCE: 53, NPE: 47})
        self.tick(runs, T(tue, "12:46"))
        self.assertEqual(sx["legs"]["ce"]["phase"], "armed")
        self.assertEqual(sx["legs"]["ce"]["trig"], 90.25)
        self.assertEqual(self.k.n, 0)
        # 13:00: every leg breaks out. Margin covers Nifty CE only after reserving for Sensex? Sensex sells first,
        # so nothing is reserved; free margin allows one Nifty leg.
        self.k.free = 450_000                         # 2 Sensex legs placed (fake doesn't deduct), then:
        self.set_px(**{SCE: 90, SPE: 80, NCE: 52, NPE: 46})
        self.tick(runs, T(tue, "13:00"))
        self.assertEqual(sx["legs"]["ce"]["phase"], "open")
        self.assertEqual(sx["legs"]["pe"]["phase"], "open")
        self.assertEqual(sx["legs"]["ce"]["sl_spot"], 82200)
        self.assertEqual(sx["legs"]["pe"]["sl_spot"], 81800)
        self.assertEqual(nf["legs"]["ce"]["phase"], "open")   # 120k * 1.05 <= 450k
        self.assertEqual(nf["legs"]["ce"]["sl_spot"], 25060)
        self.assertEqual(self.k.pos[("BFO", "SX112682300CE")], -20)
        # Nifty PE triggers later when margin is short -> skipped, never retried
        self.k.free = 100_000
        self.set_px(**{NPE: 44})
        self.tick(runs, T(tue, "13:05"))
        self.assertEqual(nf["legs"]["pe"]["phase"], "skipped_margin")
        self.assertTrue(any("margin short" in m for m in self.sent))
        self.k.free = 10_000_000
        self.tick(runs, T(tue, "13:06"))
        self.assertEqual(nf["legs"]["pe"]["phase"], "skipped_margin")
        # 13:30: Sensex up 200 -> Sensex CE stopped; Nifty up 70 -> Nifty CE stopped
        self.set_px(**{"BSE:SENSEX": 82201.0, "NSE:NIFTY 50": 25070.0, SCE: 120, NCE: 70})
        self.tick(runs, T(tue, "13:30"))
        self.assertEqual(sx["legs"]["ce"]["phase"], "wait_re")
        self.assertEqual(sx["legs"]["ce"]["trades"][0]["pnl"], (90 - 120) * 20)
        self.assertEqual(sx["legs"]["pe"]["phase"], "open")
        self.assertEqual(nf["legs"]["ce"]["phase"], "wait_re")
        self.assertEqual(self.k.pos[("BFO", "SX112682300CE")], 0)
        # 14:00: Sensex CE back at cost -> sold again with a new stop; Nifty CE not yet
        self.set_px(**{"BSE:SENSEX": 82100.0, SCE: 89.5, NCE: 55})
        self.tick(runs, T(tue, "14:00"))
        L = sx["legs"]["ce"]
        self.assertEqual((L["phase"], L["entry"], L["sl_spot"], L["reentries_left"]), ("open", 89.5, 82300, 0))
        # 15:26: past the cutoff, Nifty CE at cost but no re-entry on day 1
        self.set_px(**{NCE: 50})
        self.tick(runs, T(tue, "15:26"))
        self.assertEqual(nf["legs"]["ce"]["phase"], "wait_re")
        for st in runs:
            B.end_of_day(self.s, st, T(tue, "15:30"))
        self.assertEqual([st["status"] for st in runs], ["active", "active"])
        self.s["days"]["2"] = []                      # keep Wednesday's own new run out of this test
        # Wednesday: state reloads from disk; no new run (Wed list is Sensex only and it's before 09:45)
        runs2 = B.load_runs()
        self.assertEqual(sorted(st["index"] for st in runs2), ["NIFTY", "SENSEX"])
        nf2 = next(st for st in runs2 if st["index"] == "NIFTY")
        sx2 = next(st for st in runs2 if st["index"] == "SENSEX")
        # 09:16: Nifty CE back below cost -> re-entry allowed next morning
        self.set_px(**{"BSE:SENSEX": 82150.0, "NSE:NIFTY 50": 25040.0, NCE: 49, SCE: 85, SPE: 70})
        self.tick(runs2, T(wed, "09:16"))
        self.assertEqual(nf2["legs"]["ce"]["phase"], "open")
        self.assertEqual(nf2["legs"]["ce"]["sl_spot"], 25100)
        # 12:30: everything closed, journal written, Kite flat
        self.set_px(**{SCE: 60, SPE: 50, NCE: 30})
        self.tick(runs2, T(wed, "12:30"))
        self.assertEqual([st["status"] for st in (sx2, nf2)], ["closed", "closed"])
        self.assertTrue(all(q == 0 for q in self.k.pos.values()), self.k.pos)
        self.assertEqual(sx2["gross_pnl"], (90 - 120) * 20 + (89.5 - 60) * 20 + (80 - 50) * 20)
        self.assertEqual(nf2["gross_pnl"], (52 - 70) * 65 + (49 - 30) * 65)
        with open(os.path.join(self.tmp, B.JOURNAL_NAME)) as f:
            rows = f.read().splitlines()
        self.assertEqual(len(rows), 3)
        self.assertEqual(B.load_runs(), [])

    def test_no_breakout_means_no_trade(self):
        thu = date(2026, 11, 26)                      # Sensex expiry day -> next week's contract
        self.set_px(**{"BSE:SENSEX": 82000.0, "BFO:SX120382300CE": 100, "BFO:SX120381700PE": 100})
        runs = []
        self.tick(runs, T(thu, "09:45"))
        self.assertEqual([r["index"] for r in runs], ["SENSEX"])
        st = runs[0]
        self.assertEqual(st["expiry"], "2026-12-03")
        ce, pe = (f"BFO:{st['legs'][k]['symbol']}" for k in ("ce", "pe"))
        self.set_px(**{ce: 100, pe: 100})
        self.tick(runs, T(thu, "10:00"))
        self.set_px(**{ce: 99, pe: 120})
        self.tick(runs, T(thu, "13:00"))
        self.tick(runs, T(thu, "15:26"))
        B.end_of_day(self.s, st, T(thu, "15:30"))
        self.assertEqual(st["status"], "closed")
        self.assertEqual(self.k.n, 0)

    def test_restart_finds_sale_in_kite(self):
        tue = date(2026, 11, 24)
        self.set_px(**{"BSE:SENSEX": 82000.0, "NSE:NIFTY 50": 25000.0})
        st = B.pick(self.k, self.s, "SENSEX", T(tue, "09:45"))
        L = st["legs"]["ce"]; L.update(phase="ordering", prev_phase="armed", low=95, trig=90.25)
        key = f"BFO:{L['symbol']}"; self.k.px[key] = 90
        self.k.place_order(exchange="BFO", tradingsymbol=L["symbol"], transaction_type="SELL", quantity=20)
        B.save(st)
        B.reconcile(self.k, self.cfg, st, [st])
        self.assertEqual((L["phase"], L["entry"], L["sl_spot"]), ("open", 90, 82200))
        # and a leg that never reached Kite goes back to waiting
        P = st["legs"]["pe"]; P.update(phase="ordering", prev_phase="armed")
        B.reconcile(self.k, self.cfg, st, [st])
        self.assertEqual(P["phase"], "armed")

    def test_failed_sale_drops_leg(self):
        tue = date(2026, 11, 24)
        self.set_px(**{"BSE:SENSEX": 82000.0})
        st = B.pick(self.k, self.s, "SENSEX", T(tue, "09:45"))
        L = st["legs"]["ce"]; L.update(phase="armed", low=95, trig=90.25)
        self.k.px[f"BFO:{L['symbol']}"] = 90
        self.k.reject.add(L["symbol"])
        B.sell(self.k, self.cfg, self.s, st, "ce", 82000.0, 90, [st], T(tue, "13:00"), "test")
        self.assertEqual(L["phase"], "failed")
        self.assertTrue(any("could not sell" in m for m in self.sent))


if __name__ == "__main__":
    unittest.main()
