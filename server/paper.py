"""Paper broker: same interface as KiteConnect for orders, filled against LIVE Kite quotes. Places nothing real."""
import json, os, itertools, time
from market import last, bid, ask

class PaperKite:
    VARIETY_REGULAR = "regular"

    def __init__(self, real, market, book_file, log):
        self.real, self.mkt, self.book_file, self.log = real, market, book_file, log
        self.ord = json.load(open(book_file)) if os.path.exists(book_file) else {}
        self.ids = itertools.count(len(self.ord) + 1)

    def __getattr__(self, name):          # profile, margins, instruments, quote, ltp ... -> real Kite (read-only)
        return getattr(self.real, name)

    def _save(self):
        json.dump(self.ord, open(self.book_file + ".tmp", "w"), indent=1); os.replace(self.book_file + ".tmp", self.book_file)

    def place_order(self, **p):
        oid = f"P{next(self.ids):04d}"
        st = "TRIGGER PENDING" if p.get("order_type") == "SL" else "OPEN"
        self.ord[oid] = dict(order_id=oid, status=st, filled_quantity=0, average_price=0, placed=time.time(), **p)
        self._match(); self._save()
        return oid

    def modify_order(self, variety, order_id, price=None, trigger_price=None, **kw):
        o = self.ord[order_id]
        if o["status"] in ("COMPLETE", "CANCELLED", "REJECTED"): raise Exception("Order cannot be modified")
        if price is not None: o["price"] = price
        if trigger_price is not None: o["trigger_price"] = trigger_price
        self._match(); self._save()

    def cancel_order(self, variety, order_id):
        o = self.ord[order_id]
        if o["status"] == "COMPLETE": raise Exception("Order already complete")
        o["status"] = "CANCELLED"; self._save()

    def orders(self):
        self._match(); self._save()
        return [dict(o) for o in self.ord.values()]

    def _match(self):
        live = [o for o in self.ord.values() if o["status"] in ("OPEN", "TRIGGER PENDING")]
        if not live: return
        q = self.mkt.snap(extra=[f"NFO:{o['tradingsymbol']}" for o in live])
        for o in live:
            k = f"NFO:{o['tradingsymbol']}"
            if k not in q: continue
            lp, b, a = last(q, k), bid(q, k), ask(q, k)
            if o["status"] == "TRIGGER PENDING":
                if lp >= o["trigger_price"]:
                    o["status"] = "OPEN"; o["triggered_at"] = time.time()
                else:
                    continue
            if o["transaction_type"] == "SELL" and b >= o["price"]:
                o.update(status="COMPLETE", filled_quantity=o["quantity"], average_price=b)
            elif o["transaction_type"] == "BUY" and a <= o["price"]:
                o.update(status="COMPLETE", filled_quantity=o["quantity"], average_price=a)
