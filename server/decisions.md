# Decision log — Nifty straddle bot

Every rule change goes here: date, what changed, why, and the data behind it.
Change rule: a variant replaces the live rules only if it beat them over >= 20 trading days AND in both halves of that period. One change at a time. Reviews on Saturdays.

## 2026-10-21 — starting rules
- Sell ATM Nifty straddle at 09:20 (weekly expiry; next week's on expiry day), 1 lot.
- Stop-loss 30% per leg (SL-limit at the exchange), daily loss cap Rs 15,000, exit 15:15.
- Faster stop handling: 1.2 s polling near stops, re-price an unfilled triggered stop after 2 s.
- Basis: 60-day simulated backtest (straddle 30% SL best of the variants tested; result depended on intraday decay assumption).
- Day zero (paper, NSE public data): 21 Oct, 23050 straddle, PE stopped 10:12, CE held to 15:15 -> +Rs 4,573 gross / ~+4,378 net per lot.
- Shadow variants tracked from day 1: S25, S40, S30+breakeven, S30+profit lock, Strangle150 SL50, skip if VIX up >5% at entry.

## 2026-10-22 - fixes + shadow variants (no change to live rules)
- FIX: 09:20 entry failed - orders went out over IPv6, rejected by Kite (only IPv4 3.106.111.8 whitelisted). Bot now forces IPv4.
- Live result 22 Oct (10:00 entry, 22650): CE stopped 10:23, PE held to 15:15. Net Rs -1,038. Best +1,320 @ 11:15, worst -4,485 @ 12:19.
- Added shadows: trend exit; trend exit after 1st stop; tight SL after 1st stop; green lock after 1st stop; small profit lock.
- 2026-10-27 installed v8 extras.py (Wed Sensex fly, Thu Sensex 11:30, Fri Nifty fly)
- 2026-10-28 installed v9 extras.py: Monday Nifty fly nf_mon (paper), event filter
- 2026-10-28 bot: live exit 15:10 on Mondays (exit_time_by_day), for nf_mon at 15:13
- 2026-10-28 nf_mon set LIVE (no paper trading)
- 2026-10-28 installed v10 extras.py: bn_wed (BankNifty last-week Wed) + sx_wknd (Sensex weekend fly), LIVE
- 2026-10-29 installed v11 extras.py: morning stop on 1-min closes from 09:16, Kite-position checks before every close, leftover alert
- 2026-11-02 installed v12 extras.py: shorts closed before wings on exit/unwind, failed legs retried
- 2026-11-02 bot: move-to-cost on Mondays (move_to_cost_days [0]); backup in bak_mtc/
- 2026-11-03 installed v14 extras.py: morning_stop confirm (gap >= 0.15% against leg stops at once, else book mid >= stop on 2 reads); backup bak_v14/
- 2026-11-03 installed bias.py (OI-bias plan v1): Wed/Fri 15:15 bias-or-fly, Thu 11:00 check gates sx_thu; old sx_fly/nf_fly/sx_wknd entries off. Backup bak_bias/
- 2026-11-03 added P&L calendar page (pnl_web.py, service pnl-web, Caddy /calendar)
- 2026-11-03 P&L calendar v2: pnl_web.py + pnl_sync.py (exact Zerodha fills and charges, hourly ledger), Caddy /calendar
- 2026-11-03 P&L calendar v2: pnl_web.py + pnl_sync.py (exact Zerodha fills and charges, hourly ledger), Caddy /calendar
