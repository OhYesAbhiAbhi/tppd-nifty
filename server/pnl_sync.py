#!/usr/bin/env python3
"""Exact Zerodha P&L ledger (3 Nov 2026). Read-only: never places orders.

Each run stores today's executed F&O fills from Kite (exact prices, quantities, order tags) in ledger_fills.csv
and each order's charges in ledger_charges.csv. Charges come from Zerodha's virtual contract note API
(brokerage, STT, exchange, SEBI, stamp, GST); if that API isn't available they are computed with Zerodha's
published rates and marked "formula". Run it after the last trade of the day (cron 15:45). Safe to re-run.

  venv/bin/python pnl_sync.py                 sync today
  venv/bin/python pnl_sync.py import FILE.csv  backfill from a Console tradebook export (charges by formula)
"""
import csv, os, sys
from datetime import datetime

import extras as X

BASE = X.BASE
FILLS = os.path.join(BASE, "ledger_fills.csv")
CHG = os.path.join(BASE, "ledger_charges.csv")
FCOLS = ["trade_id", "order_id", "date", "time", "exchange", "tradingsymbol", "side", "qty", "price", "tag", "product"]
CCOLS = ["order_id", "date", "exchange", "tradingsymbol", "side", "qty", "avg_price", "charges", "source"]


def read(p):
    return list(csv.DictReader(open(p, newline=""))) if os.path.exists(p) else []


def append(p, cols, rows):
    new = not os.path.exists(p)
    with open(p, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        if new:
            w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in cols})


def formula_charges(exchange, side, qty, price):
    """Zerodha F&O options charges per order (published rates, Nov 2026)."""
    prem = qty * price
    brokerage = 20.0
    stt = 0.001 * prem if side == "SELL" else 0.0
    txn = prem * (0.0003503 if exchange == "NFO" else 0.000325)
    sebi = prem * 10 / 1e7
    stamp = 0.00003 * prem if side == "BUY" else 0.0
    gst = 0.18 * (brokerage + txn + sebi)
    return round(brokerage + stt + txn + sebi + stamp + gst, 2)


def sync():
    cfg = X.read_cfg(); kite = X.kite_login(cfg, "pnl_sync")
    today = str(X.now().date())
    have = {r["trade_id"] for r in read(FILLS)}
    orders = {str(o["order_id"]): o for o in kite.orders()}
    new = []
    for t in kite.trades():
        if t.get("exchange") not in ("NFO", "BFO") or str(t["trade_id"]) in have:
            continue
        o = orders.get(str(t["order_id"]), {})
        tag = o.get("tag") or ""
        if not tag and o.get("tags"):
            tag = o["tags"][0]
        ts = t.get("fill_timestamp") or t.get("exchange_timestamp") or t.get("order_timestamp")
        tm = ts.strftime("%H:%M:%S") if hasattr(ts, "strftime") else str(ts)[11:19]
        new.append(dict(trade_id=t["trade_id"], order_id=t["order_id"], date=today, time=tm, exchange=t["exchange"],
                        tradingsymbol=t["tradingsymbol"], side=t["transaction_type"], qty=int(t["quantity"]),
                        price=float(t["average_price"]), tag=tag, product=t.get("product", "")))
    append(FILLS, FCOLS, new)
    # charges: one row per executed order today
    done = {r["order_id"] for r in read(CHG)}
    todays = [o for o in orders.values() if o.get("status") == "COMPLETE" and o.get("exchange") in ("NFO", "BFO")
              and str(o["order_id"]) not in done]
    rows = []
    if todays:
        params = [dict(order_id=str(o["order_id"]), exchange=o["exchange"], tradingsymbol=o["tradingsymbol"],
                       transaction_type=o["transaction_type"], variety=o.get("variety", "regular"),
                       product=o["product"], order_type=o["order_type"], quantity=int(o["filled_quantity"]),
                       average_price=float(o["average_price"])) for o in todays]
        api = {}
        try:
            res = kite.get_virtual_contract_note(params)
            for p, r in zip(params, res):
                api[p["order_id"]] = float(r["charges"]["total"])
        except Exception as e:
            X.log("pnl_sync", f"contract note API unavailable ({e}) - using Zerodha's published rates")
        for p in params:
            if p["order_id"] in api:
                c, src = api[p["order_id"]], "zerodha"
            else:
                c, src = formula_charges(p["exchange"], p["transaction_type"], p["quantity"], p["average_price"]), "formula"
            rows.append(dict(order_id=p["order_id"], date=today, exchange=p["exchange"], tradingsymbol=p["tradingsymbol"],
                             side=p["transaction_type"], qty=p["quantity"], avg_price=p["average_price"], charges=c, source=src))
    append(CHG, CCOLS, rows)
    print(f"{today}: {len(new)} new fills, {len(rows)} orders' charges "
          f"({sum(1 for r in rows if r['source'] == 'zerodha')} from Zerodha, {sum(1 for r in rows if r['source'] == 'formula')} by formula)")


def import_tradebook(path):
    """Console > Reports > Tradebook (F&O) CSV: trade_date, tradingsymbol, exchange, trade_type, quantity, price,
    trade_id, order_id, order_execution_time. Charges by formula, one per order."""
    have = {r["trade_id"] for r in read(FILLS)}
    rows = list(csv.DictReader(open(path, newline="", encoding="utf-8-sig")))
    low = lambda r: {k.strip().lower(): v for k, v in r.items()}
    new, orders = [], {}
    for r in map(low, rows):
        if r.get("exchange") not in ("NFO", "BFO") or r.get("trade_id") in have:
            continue
        d = r.get("trade_date", "")[:10]
        tm = (r.get("order_execution_time") or "")[11:19] or "00:00:00"
        side = r.get("trade_type", "").upper()
        q, p = int(float(r["quantity"])), float(r["price"])
        new.append(dict(trade_id=r["trade_id"], order_id=r["order_id"], date=d, time=tm, exchange=r["exchange"],
                        tradingsymbol=r["symbol"] if "symbol" in r else r["tradingsymbol"], side=side, qty=q, price=p,
                        tag="", product="NRML"))
        o = orders.setdefault(r["order_id"], dict(order_id=r["order_id"], date=d, exchange=r["exchange"],
                                                  tradingsymbol=new[-1]["tradingsymbol"], side=side, qty=0, val=0.0))
        o["qty"] += q; o["val"] += q * p
    new.sort(key=lambda x: (x["date"], x["time"]))
    append(FILLS, FCOLS, new)
    done = {r["order_id"] for r in read(CHG)}
    ch = []
    for o in orders.values():
        if o["order_id"] in done:
            continue
        avg = o["val"] / o["qty"]
        ch.append(dict(order_id=o["order_id"], date=o["date"], exchange=o["exchange"], tradingsymbol=o["tradingsymbol"],
                       side=o["side"], qty=o["qty"], avg_price=round(avg, 4),
                       charges=formula_charges(o["exchange"], o["side"], o["qty"], avg), source="formula"))
    append(CHG, CCOLS, ch)
    print(f"imported {len(new)} fills, {len(ch)} orders")


if __name__ == "__main__":
    a = sys.argv[1:]
    if a[:1] == ["import"] and len(a) > 1:
        import_tradebook(a[1])
    else:
        sync()
