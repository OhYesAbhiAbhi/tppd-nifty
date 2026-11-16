"""
Phone page for the bot (runs on the server as a service on port 8000).

  http://<server>:8000/?key=<web_key>     status + Login / Pause / Resume buttons (bookmark this on your phone)
  /callback                               Zerodha redirects here after login -> saves today's access token
  /pause?key=..  /resume?key=..           create / remove the STOP file
  /export?key=..&f=journal.csv|shadow.csv|decisions.md|log    plain-text download for reviews
"""
import json, os, hmac, html, datetime as dt, urllib.parse, glob
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from kiteconnect import KiteConnect
from notify import tg

HERE = os.path.dirname(os.path.abspath(__file__)); os.chdir(HERE)
def cfg(): return json.load(open("config.json"))

def token_status(c):
    if not os.path.exists("access_token.txt"): return False, "not logged in"
    try:
        k = KiteConnect(api_key=c["api_key"]); k.set_access_token(open("access_token.txt").read().strip())
        p = k.profile(); m = k.margins("equity")["net"]
        return True, f"logged in as {p['user_name']} - margin Rs {m:,.0f}"
    except Exception:
        return False, "login expired - please log in"

PAGE = """<!doctype html><html><head><meta name=viewport content="width=device-width,initial-scale=1">
<title>Nifty bot</title><style>
body{{font-family:-apple-system,Segoe UI,Roboto,sans-serif;margin:0;padding:20px;background:#f6f7f9;color:#1d1f23}}
.card{{background:#fff;border-radius:14px;padding:18px;margin-bottom:14px;box-shadow:0 1px 3px rgba(0,0,0,.08)}}
h1{{font-size:20px;margin:0 0 12px}} .s{{font-size:15px;margin:6px 0}}
a.b{{display:block;text-align:center;padding:15px;border-radius:12px;text-decoration:none;font-weight:600;font-size:17px;margin-top:10px}}
.go{{background:#2563eb;color:#fff}} .pause{{background:#fee2e2;color:#991b1b}} .res{{background:#dcfce7;color:#166534}}
.ok{{color:#166534}} .bad{{color:#b91c1c}} small{{color:#666}}
</style></head><body>{body}</body></html>"""

class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def send(self, body, code=200, ctype="text/html; charset=utf-8"):
        b = body.encode(); self.send_response(code); self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store"); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
    def page(self, inner): self.send(PAGE.format(body=inner))

    def do_GET(self):
        c = cfg(); u = urllib.parse.urlparse(self.path); qs = dict(urllib.parse.parse_qsl(u.query))
        authed = bool(c.get("web_key")) and hmac.compare_digest(qs.get("key", ""), c["web_key"])
        login_url = f"https://kite.zerodha.com/connect/login?api_key={c['api_key']}&v=3"
        home = f"/?key={c.get('web_key','')}" if authed else "/"

        if u.path == "/paste":   # fallback: user pastes the full address they landed on after Zerodha login
            pq = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(qs.get("u", "").strip()).query))
            qs = {"status": pq.get("status", ""), "request_token": pq.get("request_token", "")}
            u = u._replace(path="/callback")

        if u.path == "/callback":
            if qs.get("status") != "success" or not qs.get("request_token"):
                return self.page("<div class=card><h1 class=bad>Login failed</h1><a class='b go' href='%s'>Try again</a></div>" % login_url)
            try:
                k = KiteConnect(api_key=c["api_key"]); d = k.generate_session(qs["request_token"], api_secret=c["api_secret"])
                open("access_token.txt", "w").write(d["access_token"]); os.chmod("access_token.txt", 0o600)
                k.set_access_token(d["access_token"]); m = k.margins("equity")["net"]
                mode = "PAPER" if c.get("paper") else "LIVE"
                warn = ""
                if not c.get("paper") and m < c.get("min_margin_rs", 175000):
                    warn = f"<p class='s bad'>Margin Rs {m:,.0f} is below Rs {c.get('min_margin_rs',175000):,} - live orders may be rejected.</p>"
                tg(c, f"Logged in for {dt.date.today():%d %b}. Mode {mode}. Margin Rs {m:,.0f}. Bot follows the weekday schedule.")
                return self.page(f"<div class=card><h1 class=ok>&#9989; Logged in</h1><p class=s>{html.escape(d.get('user_name',''))}</p>"
                                 f"<p class=s>Mode: <b>{mode}</b> &middot; Margin Rs {m:,.0f}</p>{warn}<p class=s>The bot trades per the weekday schedule (Mon 11:30, expiry day 9:20, Wed-Fri 10:45 if VIX calm). You can close this page.</p></div>")
            except Exception as e:
                return self.page(f"<div class=card><h1 class=bad>Login error</h1><p class=s>{html.escape(str(e))}</p><a class='b go' href='{login_url}'>Try again</a></div>")

        if u.path in ("/pause", "/resume"):
            if not authed: return self.send("forbidden", 403, "text/plain")
            if u.path == "/pause":
                open("STOP", "w").close(); tg(c, "PAUSED from phone - the bot will not trade / will exit open positions within seconds.")
            elif os.path.exists("STOP"):
                os.remove("STOP"); tg(c, "Resumed from phone - the bot will trade on the next run.")
            self.send_response(303); self.send_header("Location", home); self.end_headers(); return

        if u.path == "/data":     # collected market data (gzip CSV) for offline research; ?f=list for an index
            if not authed: return self.send("forbidden", 403, "text/plain")
            f = qs.get("f", "list")
            if f == "list":
                files = sorted(os.path.relpath(p, "data") for p in glob.glob("data/**/*.csv.gz", recursive=True))
                return self.send("\n".join(f"{p}\t{os.path.getsize(os.path.join('data', p))}" for p in files), 200, "text/plain")
            p = os.path.normpath(os.path.join("data", f))
            if not p.startswith("data" + os.sep) or not p.endswith(".csv.gz") or not os.path.exists(p):
                return self.send("not found", 404, "text/plain")
            b = open(p, "rb").read(); self.send_response(200)
            self.send_header("Content-Type", "application/gzip"); self.send_header("Content-Length", str(len(b)))
            self.send_header("Access-Control-Allow-Origin", "*"); self.end_headers(); self.wfile.write(b); return

        if u.path == "/export":
            if not authed: return self.send("forbidden", 403, "text/plain")
            f = qs.get("f", "journal.csv")
            if f == "log":
                logs = sorted(glob.glob("log_*.txt")); f = logs[-1] if logs else ""
            if f not in ("journal.csv", "shadow.csv", "decisions.md") and not (f.startswith("log_") and f.endswith(".txt")):
                return self.send("not allowed", 400, "text/plain")
            if not f or not os.path.exists(f): return self.send("(none yet)", 200, "text/plain; charset=utf-8")
            return self.send(open(f).read(), 200, "text/plain; charset=utf-8")

        if u.path == "/":
            ok, msg = token_status(c)
            mode = "PAPER" if c.get("paper") else "LIVE"
            paused = os.path.exists("STOP")
            body = f"<div class=card><h1>Nifty bot &middot; {dt.date.today():%a %d %b}</h1>"
            body += f"<p class='s {'ok' if ok else 'bad'}'>{'&#9989;' if ok else '&#9888;&#65039;'} {html.escape(msg)}</p>"
            if authed:
                body += f"<p class=s>Mode: <b>{mode}</b> &middot; {c['lots']} lot</p><p class=s><small>Expiry day 9:20 (next week) &middot; Mon 11:30 &middot; Wed-Fri 10:45 if VIX &le; its 9:15 level</small></p>"
                body += f"<p class='s {'bad' if paused else 'ok'}'>{'PAUSED - will not trade' if paused else 'Active'}</p>"
            body += f"<a class='b go' href='{login_url}'>Log in to Zerodha</a>"
            body += ("<form action='/paste' method='get' style='margin-top:14px'><small>If you land on a 'can't be reached' page after login, "
                     "copy that page's full address and paste it here:</small>"
                     "<input name='u' placeholder='https://127.0.0.1/?...request_token=...' "
                     "style='width:100%;box-sizing:border-box;padding:12px;margin-top:6px;border:1px solid #ccc;border-radius:10px;font-size:15px'>"
                     "<button style='width:100%;padding:13px;margin-top:8px;border:0;border-radius:10px;background:#111;color:#fff;font-size:16px'>Submit</button></form>")
            if authed:
                body += (f"<a class='b res' href='/resume?key={c['web_key']}'>Resume trading</a>" if paused else
                         f"<a class='b pause' href='/pause?key={c['web_key']}'>Pause / exit now</a>")
                st = sorted(glob.glob("state_*.json"))
                if st:
                    s = json.load(open(st[-1]))
                    if s.get("legs"):
                        body += "<p class=s><small>Today: " + html.escape(", ".join(f"{l['symbol']} {l.get('avg')}→{l.get('exit') or 'open'}" for l in s['legs'])) + \
                                f" &middot; MTM Rs {s.get('mtm', 0):,}</small></p>"
            body += "</div>"
            return self.page(body)
        self.send("not found", 404, "text/plain")

if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", int(cfg().get("web_port", 8000))), H).serve_forever()
