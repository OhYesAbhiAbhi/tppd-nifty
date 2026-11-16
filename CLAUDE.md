# TPPD Nifty — rules for working in this repo

This repo is the master copy of the code that trades real money through Zerodha Kite,
running on Abhineet's AWS Lightsail server (3.106.111.8, user `ubuntu`, code in `/home/ubuntu/algo`,
venv `venv/`). The same server runs the BTC bot (`~/btc_bot`, repo `tppd-btc`) — never touch its files,
services or crontab lines.

## Layout
- `server/` — the server's `~/algo` code, file for file. `straddle_live.py` = Mon/Tue Nifty straddle bot (cron 09:10),
  `extras.py` = Wed/Thu/Fri strategies and overnight flies (cron lines tagged `# extras`), `bias.py` / `oi_bias_log.py` =
  OI-bias trades and logger, `pnl_web.py` + `pnl_sync.py` = P&L calendar (`pnl-web`), phone page = `algo-web`.
- `deploy/` — systemd units, Caddyfile (shared with BTC) and the Nifty crontab lines, as on the server.
- Runbooks and studies live in the claude.ai project "Nifty Claude Bot" (nifty-bot-runbook.md, extras-runbook.md).

## Hard rules
1. **Never commit secrets.** No config.json, Kite API key/secret, access tokens, TOTP, Telegram tokens, web_key,
   state files, journals, ledgers, logs or market data. `config.example.json` holds names only.
2. **Every change goes through a branch + pull request**, with GitHub Actions green before merge.
   Never push straight to `main` except repo housekeeping.
3. **Tests first.** Each behaviour change ships with offline tests (no Kite calls, no keys).
4. **Install window:** never while the market is open (09:00–15:45 IST on trading days) or while any bot
   process from `~/algo` is running. Say in the PR whether an overnight position (fly / bias trade) will be
   open when it is installed and whether the change affects how that position is exited.
5. **Releases are tags** (`r1`, `r2`, …), created and pushed by the server when it deploys. r1 = the first snapshot.
6. Strategies go straight to live (no paper trading) — so be conservative and explicit about risk in every PR.
7. Plain-language summaries for Abhineet: what changes for trading, what to paste, what he should see.

## Installing (until deploy tooling exists)
Changes are installed by pasting a script into the Lightsail browser terminal. Every installer backs up the files it
replaces (`~/algo/bak_<name>/`) and logs one line to `decisions.md`.
