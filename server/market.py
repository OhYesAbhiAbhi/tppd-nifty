"""One shared, rate-limited market-data snapshot (Kite quote API allows ~1 request/second)."""
import time

class Market:
    def __init__(self, kite, min_gap=1.05):
        self.k, self.min_gap = kite, min_gap
        self.keys = set(["NSE:NIFTY 50", "NSE:INDIA VIX"])
        self.cache, self.t = {}, 0.0

    def add(self, keys):
        self.keys |= set(keys)

    def snap(self, extra=(), max_age=None):
        want = self.keys | set(extra)
        age = time.time() - self.t
        if want <= set(self.cache) and age < (self.min_gap if max_age is None else max_age):
            return self.cache
        if age < self.min_gap:
            time.sleep(self.min_gap - age)
        self.keys |= want
        q = self.k.quote(sorted(self.keys))
        self.cache, self.t = q, time.time()
        return q

def last(q, key):
    return q[key]["last_price"]

def bid(q, key):
    try:
        b = q[key]["depth"]["buy"][0]["price"]
        return b if b > 0 else q[key]["last_price"]
    except Exception:
        return q[key]["last_price"]

def ask(q, key):
    try:
        a = q[key]["depth"]["sell"][0]["price"]
        return a if a > 0 else q[key]["last_price"]
    except Exception:
        return q[key]["last_price"]
