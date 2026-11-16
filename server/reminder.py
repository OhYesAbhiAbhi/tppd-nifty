"""Cron at 08:45 and 09:05 on weekdays: if today's Kite login hasn't been done, nag on Telegram with the login link."""
import json, os, datetime as dt
from kiteconnect import KiteConnect
from notify import tg

os.chdir(os.path.dirname(os.path.abspath(__file__)))
c = json.load(open("config.json"))
if os.path.exists("STOP"):
    raise SystemExit
ok = False
try:
    k = KiteConnect(api_key=c["api_key"]); k.set_access_token(open("access_token.txt").read().strip())
    k.profile(); ok = True; m = k.margins("equity")["net"]
except Exception:
    pass
page = f"{c.get('public_url', 'http://3.106.111.8:8000')}/?key={c.get('web_key', '')}"
if not ok:
    tg(c, f"Kite login needed before {c['entry_time']} or the bot skips today.\nTap: {page}")
elif not c.get("paper") and m < c.get("min_margin_rs", 175000):
    tg(c, f"Logged in, but margin is Rs {m:,.0f} (need ~Rs {c.get('min_margin_rs',175000):,}). Live orders may be rejected today.")
