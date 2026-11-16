# TPPD Nifty bot

Private. Master copy of the code that runs on the Lightsail server in `/home/ubuntu/algo`
(Zerodha Kite: Nifty straddle bot, extras.py flies/straddles, OI-bias trades, P&L calendar, phone page).

- `server/` - the server's `~/algo` code as it runs (no config.json, tokens, state, journals, data or logs)
- `server/config.example.json` - config.json with every key/token/password blanked
- `deploy/` - systemd units, Caddyfile and the Nifty crontab lines, as on the server; `snapshot.sh` made this repo
- `CLAUDE.md` - working rules: branches + pull requests, tests on GitHub, release tags, install window

Snapshot taken 16 Nov 2026 23:30 IST from ip-172-26-11-24.
Secrets live only on the server in `~/algo/config.json` and are never committed.
