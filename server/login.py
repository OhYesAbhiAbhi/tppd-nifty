"""
Daily Kite login. Run once each morning before 9:15.

  python login.py

1. It prints a Zerodha login URL. Open it, log in (with your TOTP).
2. You land on your redirect URL (e.g. http://127.0.0.1/?request_token=XXXX&...).
   The page may show "can't connect" - that's fine. Copy the request_token value from the address bar.
3. Paste it here. The access token is saved to access_token.txt (valid until ~6 AM next day).
"""
import json, sys
from kiteconnect import KiteConnect

cfg = json.load(open("config.json"))
kite = KiteConnect(api_key=cfg["api_key"])
print("\nOpen this URL and log in:\n\n ", kite.login_url(), "\n")
token = (sys.argv[1] if len(sys.argv) > 1 else input("Paste request_token: ")).strip()
data = kite.generate_session(token, api_secret=cfg["api_secret"])
open("access_token.txt", "w").write(data["access_token"])
kite.set_access_token(data["access_token"])
p = kite.profile()
m = kite.margins("equity")
print(f"\nLogged in as {p['user_name']} ({p['user_id']}). Available margin: Rs {m['net']:,.0f}")
