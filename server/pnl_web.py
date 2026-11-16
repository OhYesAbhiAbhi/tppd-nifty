#!/usr/bin/env python3
"""P&L calendar page for the Nifty bot server (stdlib only).

Reads journal.csv (straddle bot, net P&L) and extras_journal.csv (extras.py + bias.py, gross P&L before charges)
and shows a month grid: each day's total and every strategy that closed that day.
Overnight trades are counted on the day they were closed.

Runs on 127.0.0.1:8001; Caddy forwards https://<server>/calendar to it. Needs ?key=<web_key from config.json>.
  venv/bin/python pnl_web.py            serve
  venv/bin/python pnl_web.py dump       print the parsed rows (debug)
"""
import calendar, csv, hmac, html, json, os, sys
from collections import defaultdict
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse, urlencode

BASE = os.path.dirname(os.path.abspath(__file__))
PORT = 8001
NAMES = {
    "bot": "Nifty straddle", "nf_mon": "Mon Nifty fly", "nf_fly": "Fri Nifty fly", "nff_fly": "Fri Nifty fly",
    "sx_fly": "Wed Sensex fly", "nfw_fly": "Wed Nifty fly", "sx_thu": "Thu Sensex straddle",
    "bn_wed": "BankNifty straddle", "sx_wknd": "Fri Sensex fly", "sxf_fly": "Fri Sensex fly",
    "nf_wed_bias": "Wed Nifty bias", "nf_fri_bias": "Fri Nifty bias", "sx_fri_bias": "Fri Sensex bias", "manual": "Manual",
}
SHORT = {"bot": "NF strd", "nf_mon": "NF fly", "nf_fly": "NF fly", "nff_fly": "NF fly", "sx_fly": "SX fly",
         "nfw_fly": "NF fly", "sx_thu": "SX strd", "bn_wed": "BN strd", "sx_wknd": "SX fly", "sxf_fly": "SX fly",
         "nf_wed_bias": "NF bias", "nf_fri_bias": "NF bias", "sx_fri_bias": "SX bias", "manual": "Manual"}


def web_key():
    try:
        c = json.load(open(os.path.join(BASE, "config.json")))
        return str(c.get("web_key") or c.get("webkey") or c.get("key") or "")
    except Exception:
        return ""


def num(x):
    try:
        return float(str(x).replace(",", "").replace("₹", "").strip())
    except Exception:
        return None


def pday(x):
    x = str(x or "").strip()[:10]
    for f in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%Y%m%d"):
        try:
            return datetime.strptime(x, f).date()
        except Exception:
            pass
    return None


def truthy(x):
    return str(x).strip().lower() in ("1", "true", "yes", "y", "paper")


def load_ledger():
    """Exact rows from ledger_fills.csv / ledger_charges.csv (pnl_sync.py): FIFO-matched realised P&L on the
    closing day minus each order's Zerodha charges on its day. Returns (rows, first ledger day) or ([], None)."""
    fp, cp = os.path.join(BASE, "ledger_fills.csv"), os.path.join(BASE, "ledger_charges.csv")
    if not os.path.exists(fp):
        return [], None
    fills = sorted(csv.DictReader(open(fp, newline="")), key=lambda r: (r["date"], r["time"], r["trade_id"]))
    if not fills:
        return [], None
    lots, out, ostrat = {}, defaultdict(float), {}
    for f in fills:
        d, sym, q, px = pday(f["date"]), f["tradingsymbol"], int(float(f["qty"])), float(f["price"])
        sgn = 1 if f["side"].upper() == "BUY" else -1
        book = lots.setdefault(sym, [])
        tag = (f.get("tag") or "").strip()
        closing = bool(book) and (book[0][0] > 0) != (sgn > 0)
        own = tag if tag in NAMES else (book[0][2] if book else ("bot" if sym.startswith("NIFTY") and f["exchange"] == "NFO" else "manual"))
        ostrat.setdefault(f["order_id"], book[0][2] if closing else own)   # charges go to the strategy whose position it closes
        left = q
        while left and book and (book[0][0] > 0) != (sgn > 0):
            lq, lp, ls = book[0]
            m = min(left, abs(lq))
            pnl = (lp - px) * m if lq < 0 else (px - lp) * m      # short lot: sold lp, bought px / long lot: bought lp, sold px
            out[(d, ls)] += pnl
            left -= m
            book[0][0] = lq + m if lq < 0 else lq - m
            if book[0][0] == 0:
                book.pop(0)
        if left:
            book.append([sgn * left, px, own])
    if os.path.exists(cp):
        for c in csv.DictReader(open(cp, newline="")):
            out[(pday(c["date"]), ostrat.get(c["order_id"], "manual"))] -= float(c["charges"] or 0)
    rows = [(d, s, round(v, 2), "exact", False) for (d, s), v in out.items()]
    return rows, min(pday(f["date"]) for f in fills)


def load():
    exact, start = load_ledger()
    rows = list(exact)
    p = os.path.join(BASE, "journal.csv")
    if os.path.exists(p):
        with open(p, newline="") as f:
            r = csv.DictReader(f)
            cols = [c for c in (r.fieldnames or [])]
            low = {c: c.lower().strip() for c in cols}
            dcol = next((c for c in cols if low[c] in ("date", "day", "trade_date")), None) or \
                next((c for c in cols if "date" in low[c]), cols[0] if cols else None)
            pcol = next((c for c in cols if low[c] in ("net_pnl", "net", "net_p&l", "netpnl", "net pnl")), None) or \
                next((c for c in cols if "net" in low[c] and "pnl" in low[c].replace("&", "n")), None) or \
                next((c for c in cols if "net" in low[c]), None) or next((c for c in cols if "pnl" in low[c]), None)
            mcol = next((c for c in cols if low[c] in ("paper", "mode")), None)
            for row in r:
                d, v = pday(row.get(dcol)), num(row.get(pcol)) if pcol else None
                if d and v not in (None, 0.0) and not (start and d >= start):
                    paper = truthy(row.get(mcol)) if mcol else False
                    rows.append((d, "bot", v, "net", paper))
    p = os.path.join(BASE, "extras_journal.csv")
    if os.path.exists(p):
        with open(p, newline="") as f:
            for row in csv.DictReader(f):
                d = pday(row.get("exit_date")) or pday(row.get("entry_date"))
                v = num(row.get("gross_pnl"))
                if d and v is not None and not (start and d >= start):
                    rows.append((d, row.get("strategy", "?"), v, "gross", truthy(row.get("paper"))))
    return rows


def money(v):
    s = f"{abs(v):,.0f}"
    return ("+" if v > 0 else "−" if v < 0 else "") + "₹" + s


CSS = """
:root{--bg:#f4f5f7;--card:#fff;--ink:#1d2330;--mut:#6b7385;--line:#e3e6ec;--pos:#0b7a43;--neg:#c0392b;
--posbg:#e7f6ee;--negbg:#fdecea;--wk:#eef0f3}
@media (prefers-color-scheme:dark){:root{--bg:#121418;--card:#1c1f25;--ink:#e8eaee;--mut:#9aa1ad;--line:#2c3038;
--pos:#4cc38a;--neg:#ff7b6b;--posbg:#16301f;--negbg:#3a1d1a;--wk:#181b20}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.35 -apple-system,Segoe UI,Roboto,Arial,sans-serif}
.wrap{max-width:1100px;margin:0 auto;padding:16px}
.top{display:flex;align-items:center;justify-content:space-between;gap:8px;flex-wrap:wrap;margin-bottom:12px}
h1{font-size:20px;margin:0}.nav a{display:inline-block;padding:6px 12px;border:1px solid var(--line);border-radius:8px;
color:var(--ink);text-decoration:none;background:var(--card);margin-left:4px}
.kpis{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-bottom:12px}
.k{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px 12px}.k .l{font-size:12px;color:var(--mut)}
.k .v{font-size:18px;font-weight:700;margin-top:2px}
.grid{display:grid;grid-template-columns:repeat(7,minmax(0,1fr));gap:6px}
.dow{font-size:12px;color:var(--mut);text-align:center;padding:2px 0}
.c{background:var(--card);border:1px solid var(--line);border-radius:10px;min-height:92px;padding:6px 7px;overflow:hidden}
.c.wk{background:var(--wk)}.c.out{visibility:hidden}.c.pos{background:var(--posbg)}.c.neg{background:var(--negbg)}
.c.today{outline:2px solid #3b6fd8}
.d{font-size:12px;color:var(--mut);display:flex;justify-content:space-between}
.t{font-weight:700;font-size:15px;margin:2px 0 3px}
.s{font-size:11.5px;display:flex;justify-content:space-between;gap:4px;border-top:1px dashed var(--line);padding-top:2px;margin-top:2px}
.s span:first-child{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--mut);min-width:0}
.s b{font-weight:400}.sn{display:none}.v{white-space:nowrap}.t{white-space:nowrap}
.pos .t,.s .p{color:var(--pos)}.neg .t,.s .n{color:var(--neg)}
.paper{opacity:.55;font-style:italic}
.tbl{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);border-radius:10px;margin-top:14px;font-size:13px}
.tbl th,.tbl td{padding:7px 10px;border-top:1px solid var(--line);text-align:left}.tbl td.r,.tbl th.r{text-align:right}
.note{color:var(--mut);font-size:12px;margin-top:10px}
@media (max-width:640px){.wrap{padding:10px}.kpis{grid-template-columns:repeat(2,1fr)}
.grid{gap:4px;grid-template-columns:repeat(5,minmax(0,1fr))}.wke,.wkd,.emp{display:none}
.c{min-height:74px;padding:5px;border-radius:7px}.t{font-size:13px}.d{font-size:11px}.dow{font-size:11px}
.s{display:block;font-size:10.5px}.s span{display:block}.fn{display:none}.sn{display:inline}}
"""


def page(key, ym):
    rows = load()
    today = date.today()
    try:
        y, m = map(int, ym.split("-"))
        date(y, m, 1)
    except Exception:
        y, m = today.year, today.month
    by = defaultdict(list)
    for d, s, v, kind, paper in rows:
        by[d].append((s, v, kind, paper))
    live = lambda items: sum(v for s, v, k, p in items if not p)
    mdays = [d for d in by if d.year == y and d.month == m]
    mtot = sum(live(by[d]) for d in mdays)
    traded = [d for d in mdays if any(not p for _, _, _, p in by[d])]
    wins = sum(1 for d in traded if live(by[d]) > 0)
    alltot = sum(v for _, _, v, _, p in rows if not p)
    per = defaultdict(lambda: [0.0, 0, 0])
    for d in mdays:
        for s, v, k, p in by[d]:
            if not p:
                per[s][0] += v; per[s][1] += 1; per[s][2] += v > 0
    prev = date(y - (m == 1), 12 if m == 1 else m - 1, 1); nxt = date(y + (m == 12), 1 if m == 12 else m + 1, 1)
    q = lambda d: "?" + urlencode({"key": key, "m": f"{d.year}-{d.month:02d}"})
    cells = []
    for wk in calendar.Calendar(0).monthdatescalendar(y, m):
        emp = "" if any(d.month == m and d.weekday() < 5 for d in wk) else " emp"
        for d in wk:
            if d.month != m:
                cells.append(f'<div class="c out{" wke" if d.weekday() >= 5 else ""}{emp}"></div>'); continue
            items = by.get(d, [])
            tot = live(items)
            cls = ["c"] + (["wk wke"] if d.weekday() >= 5 else []) + ([emp.strip()] if emp else []) + (["pos"] if tot > 0 else ["neg"] if tot < 0 else []) \
                + (["today"] if d == today else [])
            body = ""
            if items:
                body += f'<div class="t">{money(tot)}</div>' if any(not p for *_, p in items) else ""
                for s, v, k, p in sorted(items, key=lambda i: -abs(i[1])):
                    star = "" if k == "exact" else "~"
                    nm = NAMES.get(s, s) + star; sn = SHORT.get(s, s) + star
                    body += (f'<div class="s{" paper" if p else ""}"><span title="{html.escape(nm)}"><b class="fn">{html.escape(nm)}</b>'
                             f'<b class="sn">{html.escape(sn)}</b>{" (paper)" if p else ""}</span>'
                             f'<span class="v {"p" if v > 0 else "n" if v < 0 else ""}">{money(v)}</span></div>')
            cells.append(f'<div class="{" ".join(cls)}"><div class="d"><span>{d.day}</span></div>{body}</div>')
    trs = "".join(f"<tr><td>{html.escape(NAMES.get(s, s))}</td><td class='r'>{n}</td><td class='r'>{w}/{n}</td>"
                  f"<td class='r' style='color:var(--{'pos' if v > 0 else 'neg'})'>{money(v)}</td></tr>"
                  for s, (v, n, w) in sorted(per.items(), key=lambda kv: -kv[1][0]))
    dows = "".join(f'<div class="dow{" wkd" if x in ("Sat", "Sun") else ""}">{x}</div>' for x in ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"))
    col = lambda v: f"style='color:var(--{'pos' if v > 0 else 'neg' if v < 0 else 'ink'})'"
    return f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>P&amp;L calendar</title><style>{CSS}</style></head><body><div class="wrap">
<div class="top"><h1>P&amp;L · {calendar.month_name[m]} {y}</h1><div class="nav"><a href="{q(prev)}">‹ {calendar.month_abbr[prev.month]}</a>
<a href="{q(date(today.year, today.month, 1))}">Today</a><a href="{q(nxt)}">{calendar.month_abbr[nxt.month]} ›</a></div></div>
<div class="kpis"><div class="k"><div class="l">This month</div><div class="v" {col(mtot)}>{money(mtot)}</div></div>
<div class="k"><div class="l">Trading days</div><div class="v">{len(traded)}</div></div>
<div class="k"><div class="l">Winning days</div><div class="v">{wins}/{len(traded)}</div></div>
<div class="k"><div class="l">All time (live)</div><div class="v" {col(alltot)}>{money(alltot)}</div></div></div>
<div class="grid">{dows}{''.join(cells)}</div>
<table class="tbl"><tr><th>Strategy ({calendar.month_abbr[m]})</th><th class="r">Trades</th><th class="r">Wins</th><th class="r">P&amp;L</th></tr>{trs or "<tr><td colspan=4>No trades this month</td></tr>"}</table>
<div class="note">Values are exact Zerodha fills minus Zerodha charges (brokerage, STT, exchange, SEBI, stamp, GST), from the daily ledger.
Profit is counted on the day a trade is closed; each day's charges on the day they were paid. ~ = from the bot's own journal (before the ledger started; approximate).
Paper trades are greyed and not counted. Updated {datetime.now():%d %b %H:%M}.</div>
</div></body></html>"""


class H(BaseHTTPRequestHandler):
    def do_GET(self):
        u = urlparse(self.path); qs = parse_qs(u.query)
        k = web_key(); given = (qs.get("key") or [""])[0]
        if not k or not hmac.compare_digest(given, k):
            self.send_response(403); self.end_headers(); self.wfile.write(b"forbidden"); return
        try:
            body = page(given, (qs.get("m") or [""])[0]).encode()
            self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store"); self.end_headers(); self.wfile.write(body)
        except Exception as e:
            self.send_response(500); self.end_headers(); self.wfile.write(f"error: {e}".encode())

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    if sys.argv[1:] == ["dump"]:
        for r in sorted(load()):
            print(r)
    else:
        ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
