"""Offline tests for the v16 index-confirmed morning stop on overnight flies (no Kite, no keys)."""
import tempfile
import unittest
from datetime import timedelta

import extras as X
import bias as B


class FakeKite:
    def __init__(self, px):
        self.px = px

    def ltp(self, keys):
        return {k: {"last_price": self.px[k]} for k in keys}

    def place_order(self, **k):
        raise AssertionError("no orders in paper mode")


def fly(spot):
    return {"status": "open", "paper": True, "exchange": "NFO", "qty": 65, "spot": spot, "date": "2026-11-20",
            "legs": {"long_ce": {"symbol": "LC", "side": "BUY", "entry": 6.85, "open": True},
                     "long_pe": {"symbol": "LP", "side": "BUY", "entry": 16.3, "open": True},
                     "short_ce": {"symbol": "SC", "side": "SELL", "entry": 107.25, "open": True},
                     "short_pe": {"symbol": "SP", "side": "SELL", "entry": 126.35, "open": True}}}


class IndexConfirmTest(unittest.TestCase):
    def setUp(self):
        self.saved = (X.BASE, X.telegram, X.log)
        X.BASE = tempfile.mkdtemp()
        self.sent = []
        X.telegram = lambda c, n, t: self.sent.append(t)
        X.log = lambda n, m: None
        self.s = dict(B.FLY["nff_fly"], limit_buffer_pct=3.0, paper=True)

    def tearDown(self):
        X.BASE, X.telegram, X.log = self.saved

    def run_watch(self, px, st, index_pts):
        k = FakeKite(px)
        X.watch(k, {}, "nff_fly", self.s, st, X.now() + timedelta(seconds=1), index_pts=index_pts)
        return st

    def test_setting_is_on_for_friday_nifty_fly_only(self):
        self.assertEqual(B.FLY["nff_fly"]["index_confirm_pts"], 100)
        self.assertNotIn("index_confirm_pts", B.FLY["nfw_fly"])
        self.assertNotIn("index_confirm_pts", B.FLY["sxf_fly"])

    def test_spike_with_flat_index_is_ignored(self):           # 23 Nov: call printed 185+, Nifty flat
        st = self.run_watch({"NFO:SC": 185.0, "NFO:SP": 65.0, "NSE:NIFTY 50": 25810.0}, fly(25800.0), 100)
        self.assertTrue(st["legs"]["short_ce"]["open"])
        self.assertTrue(any("spike ignored" in m for m in self.sent))

    def test_real_gap_still_stops(self):
        st = self.run_watch({"NFO:SC": 185.0, "NFO:SP": 65.0, "NSE:NIFTY 50": 25930.0}, fly(25800.0), 100)
        self.assertFalse(st["legs"]["short_ce"]["open"])
        self.assertEqual(st["legs"]["short_ce"]["exit"], 185.0)
        self.assertTrue(st["legs"]["short_pe"]["open"])          # put below its stop: untouched

    def test_put_side_needs_index_down(self):
        st = self.run_watch({"NFO:SC": 60.0, "NFO:SP": 170.0, "NSE:NIFTY 50": 25690.0}, fly(25800.0), 100)
        self.assertFalse(st["legs"]["short_pe"]["open"])
        st = self.run_watch({"NFO:SC": 60.0, "NFO:SP": 170.0, "NSE:NIFTY 50": 25750.0}, fly(25800.0), 100)
        self.assertTrue(st["legs"]["short_pe"]["open"])

    def test_unreadable_index_or_missing_spot_falls_back_to_old_stop(self):
        st = self.run_watch({"NFO:SC": 185.0, "NFO:SP": 65.0}, fly(25800.0), 100)      # no index quote
        self.assertFalse(st["legs"]["short_ce"]["open"])
        st = fly(None)
        st = self.run_watch({"NFO:SC": 185.0, "NFO:SP": 65.0, "NSE:NIFTY 50": 25800.0}, st, 100)
        self.assertFalse(st["legs"]["short_ce"]["open"])

    def test_without_setting_behaviour_unchanged(self):
        st = self.run_watch({"NFO:SC": 185.0, "NFO:SP": 65.0, "NSE:NIFTY 50": 25800.0}, fly(25800.0), None)
        self.assertFalse(st["legs"]["short_ce"]["open"])


if __name__ == "__main__":
    unittest.main()
