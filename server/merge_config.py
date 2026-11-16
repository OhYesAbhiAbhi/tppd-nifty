import json, secrets
c = json.load(open("config.json"))
defaults = {"paper": True, "fast_poll_seconds": 1.2, "sl_chase_seconds": 2, "web_port": 8000,
            "public_url": "http://3.106.111.8:8000", "min_margin_rs": 175000}
for k, v in defaults.items(): c.setdefault(k, v)
if c.get("fast_poll_seconds") == 1: c["fast_poll_seconds"] = 1.2
c.setdefault("web_key", secrets.token_urlsafe(12))
json.dump(c, open("config.json", "w"), indent=2)
print("CONFIG OK  mode:", "PAPER" if c["paper"] else "LIVE", " telegram:", "yes" if c.get("telegram_chat_id") else "not yet")
