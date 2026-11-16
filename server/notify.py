"""Telegram alerts + charges estimate."""
import json, urllib.request, urllib.parse, logging

def tg(cfg, text):
    tok, chat = cfg.get("telegram_token"), cfg.get("telegram_chat_id")
    if not tok or not chat: return False
    try:
        data = urllib.parse.urlencode({"chat_id": chat, "text": text[:4000], "disable_web_page_preview": "true"}).encode()
        urllib.request.urlopen(f"https://api.telegram.org/bot{tok}/sendMessage", data, timeout=10).read()
        return True
    except Exception as e:
        logging.getLogger("bot").warning(f"telegram failed: {e}")
        return False

def charges(sell_value, buy_value, n_orders):
    """Approximate Zerodha F&O option charges in Rs. sell_value/buy_value = premium x qty."""
    brokerage = 20.0 * n_orders
    stt = 0.001 * sell_value                       # sell side, on premium
    txn = 0.0003503 * (sell_value + buy_value)     # NSE transaction charges
    sebi = 0.000001 * (sell_value + buy_value)
    stamp = 0.00003 * buy_value
    gst = 0.18 * (brokerage + txn + sebi)
    return round(brokerage + stt + txn + sebi + stamp + gst, 2)
