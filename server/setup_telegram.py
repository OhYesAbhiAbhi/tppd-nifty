"""One-time: paste your BotFather token; finds your chat (send the bot 'hi' first) and saves both to config.json."""
import json, urllib.request, sys
tok = input("Paste the Telegram bot token from BotFather and press Enter: ").strip()
try:
    r = json.load(urllib.request.urlopen(f"https://api.telegram.org/bot{tok}/getUpdates", timeout=15))
except Exception as e:
    sys.exit(f"Token not accepted by Telegram ({e}). Check it and run again.")
chats = [u["message"]["chat"] for u in r.get("result", []) if "message" in u]
if not chats:
    sys.exit("No messages found. Open your bot in Telegram, send it 'hi', then run this again.")
chat = chats[-1]
c = json.load(open("config.json")); c["telegram_token"] = tok; c["telegram_chat_id"] = chat["id"]
json.dump(c, open("config.json", "w"), indent=2)
from notify import tg
print("Saved. Test message sent." if tg(c, "Nifty bot connected. You'll get login reminders, trades and the daily summary here.") else "Saved, but the test message failed.")
