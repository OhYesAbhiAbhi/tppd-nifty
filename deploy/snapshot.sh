#!/bin/bash
# Push the Nifty bot code (~/algo) to GitHub: OhYesAbhiAbhi/tppd-nifty  (16 Nov 2026)
# READ-ONLY for trading: copies files, restarts nothing, changes nothing in ~/algo. Safe to run any time.
# Never sends config.json, tokens, state, logs, journals, CSVs, data/, venv/ or bak_*/ folders.
# Safe to re-run: the first run fills `main`; later runs push to a branch server-snapshot-YYYYMMDD-HHMM.
set -u
ALGO=${SNAP_ALGO:-/home/ubuntu/algo}
REPO=${SNAP_REPO:-$HOME/tppd-nifty-repo}
KEY=$HOME/.ssh/tppd_nifty_deploy
GH=${SNAP_GH:-git@github-nifty:OhYesAbhiAbhi/tppd-nifty.git}
STAGE=$(mktemp -d /tmp/nifty_snap.XXXXXX)
STAMP=$(TZ=Asia/Kolkata date +%Y%m%d-%H%M)
fail() { echo; echo "SNAPSHOT STOPPED - $*  (nothing was pushed; ~/algo untouched)"; rm -rf "$STAGE"; exit 1; }
[ -d "$ALGO" ] || fail "no $ALGO"
command -v git >/dev/null || fail "git is not installed (sudo apt install -y git)"

# ---------- 1. deploy key just for this repo (the BTC key is left alone) ----------
mkdir -p ~/.ssh && chmod 700 ~/.ssh
[ -f "$KEY" ] || ssh-keygen -q -t ed25519 -N "" -f "$KEY" -C "lightsail-tppd-nifty"
if ! grep -q "Host github-nifty" ~/.ssh/config 2>/dev/null; then
  printf '\nHost github-nifty\n  HostName github.com\n  User git\n  IdentityFile %s\n  IdentitiesOnly yes\n  StrictHostKeyChecking accept-new\n' "$KEY" >> ~/.ssh/config
  chmod 600 ~/.ssh/config
fi
tries=0
until git ls-remote "$GH" >/dev/null 2>&1; do
  tries=$((tries+1)); [ $tries -gt 3 ] && fail "GitHub still refuses this server's key"
  echo
  echo "=== ONE-TIME STEP: give this server access to the tppd-nifty repo ==="
  echo "1. Open https://github.com/OhYesAbhiAbhi/tppd-nifty/settings/keys/new"
  echo "   (if that page says 404, first create the repo at https://github.com/new : name tppd-nifty, Private, no README)"
  echo "2. Title: lightsail-server     Key: copy the whole line below"
  echo "3. Tick  [x] Allow write access   ->  Add key"
  echo
  cat "$KEY.pub"
  echo
  read -r -p "Press Enter once the key is added... " _
done
echo "GitHub access OK"

# ---------- 2. copy the code into a clean staging folder ----------
mkdir -p "$STAGE/server" "$STAGE/deploy/systemd" "$STAGE/.github/workflows"
cd "$ALGO"
find . \( -name venv -o -name .venv -o -name data -o -name 'data_*' -o -name 'bak*' -o -name __pycache__ \
          -o -name .git -o -name logs -o -name node_modules \) -prune -o -type f -size -1000k \
       \( -name '*.py' -o -name '*.html' -o -name '*.css' -o -name '*.js' -o -name '*.md' -o -name '*.sh' \
          -o -name 'requirements*.txt' \) -print \
  | grep -v -E '/install_[^/]*$|/snapshot_nifty\.sh$' | sed 's|^\./||' | sort > "$STAGE/.files"
while read -r f; do mkdir -p "$STAGE/server/$(dirname "$f")"; cp -p "$f" "$STAGE/server/$f"; done < "$STAGE/.files"
[ -f "$STAGE/server/requirements.txt" ] || { [ -x venv/bin/pip ] && venv/bin/pip freeze 2>/dev/null | grep -v '^-e' > "$STAGE/server/requirements.txt"; }
echo "code files copied: $(wc -l < "$STAGE/.files")"

# systemd units that run from ~/algo, Caddy config, the Nifty crontab lines (BTC lines left out)
for u in /etc/systemd/system/*.service; do grep -q "/home/ubuntu/algo" "$u" 2>/dev/null && cp "$u" "$STAGE/deploy/systemd/"; done
[ -f /etc/caddy/Caddyfile ] && cp /etc/caddy/Caddyfile "$STAGE/deploy/Caddyfile"
crontab -l 2>/dev/null | grep -v -i -E 'btc_bot|tppd' | grep -E 'algo|^[A-Z_]+=' > "$STAGE/deploy/crontab-nifty.txt"
cp "$0" "$STAGE/deploy/snapshot.sh" 2>/dev/null

# ---------- 3. config.example.json (names only) + blank secrets everywhere ----------
python3 - "$ALGO" "$STAGE" <<'PYEOF' || fail "secret check failed"
import json, os, re, sys
algo, stage = sys.argv[1], sys.argv[2]
SECRETY = re.compile(r"key|secret|token|pass|totp|pin|chat|user|api|auth|session|cookie|webhook|client", re.I)
secrets = set()

def collect(v, k=""):
    if isinstance(v, dict):
        for kk, x in v.items(): collect(x, kk)
    elif isinstance(v, list):
        for x in v: collect(x, k)
    elif not isinstance(v, bool) and v is not None and SECRETY.search(k) and len(str(v)) >= 6:
        secrets.add(str(v))

def blank(v, k=""):
    if isinstance(v, dict):
        return {kk: blank(x, kk) for kk, x in v.items()}
    if isinstance(v, list):
        return [blank(x, k) for x in v]
    if SECRETY.search(k) and not isinstance(v, bool) and v not in (None, ""):
        return "" if isinstance(v, str) else 0
    return v

for f in sorted(os.listdir(algo)):
    p = os.path.join(algo, f)
    if not os.path.isfile(p): continue
    if f.endswith(".json"):
        try: collect(json.load(open(p)))
        except Exception: pass
    if f == ".env" or f.startswith(".env"):
        for line in open(p, errors="ignore"):
            if "=" in line and not line.lstrip().startswith("#"):
                v = line.split("=", 1)[1].strip().strip("'\"")
                if len(v) >= 6: secrets.add(v)
    if re.search(r"token|session|secret|cred", f, re.I) and os.path.getsize(p) < 20000:
        s = open(p, errors="ignore").read().strip()
        if 6 <= len(s) <= 400 and "\n" not in s: secrets.add(s)
        try: collect(json.loads(s), "token")
        except Exception: pass

cfg = os.path.join(algo, "config.json")
if os.path.exists(cfg):
    json.dump(blank(json.load(open(cfg))), open(os.path.join(stage, "server", "config.example.json"), "w"), indent=2)

tg = re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b")
hits = []
for root, _, files in os.walk(stage):
    for f in files:
        p = os.path.join(root, f)
        try: s = open(p, errors="strict").read()
        except Exception: continue
        new = tg.sub("<REDACTED>", s)
        for x in sorted(secrets, key=len, reverse=True):
            new = new.replace(x, "<REDACTED>")
        if new != s:
            open(p, "w").write(new); hits.append(os.path.relpath(p, stage))
print(f"secret values checked: {len(secrets)}; files with a secret blanked out: {', '.join(hits) or 'none'}")
open(os.path.join(stage, ".redacted"), "w").write("\n".join(hits))
PYEOF

# ---------- 4. repo docs, rules, CI ----------
cat > "$STAGE/.gitignore" <<'GIEOF'
config.json
.env
.env.*
*token*
*session*
*state*.json
*.bak*
*.log*
log_*.txt
*.csv
*.db
*.sqlite*
data/
data_*/
bak*/
venv/
.venv/
__pycache__/
*.pyc
GIEOF

cat > "$STAGE/README.md" <<MDEOF
# TPPD Nifty bot

Private. Master copy of the code that runs on the Lightsail server in \`/home/ubuntu/algo\`
(Zerodha Kite: Nifty straddle bot, extras.py flies/straddles, OI-bias trades, P&L calendar, phone page).

- \`server/\` - the server's \`~/algo\` code as it runs (no config.json, tokens, state, journals, data or logs)
- \`server/config.example.json\` - config.json with every key/token/password blanked
- \`deploy/\` - systemd units, Caddyfile and the Nifty crontab lines, as on the server; \`snapshot.sh\` made this repo
- \`CLAUDE.md\` - working rules: branches + pull requests, tests on GitHub, release tags, install window

Snapshot taken $(TZ=Asia/Kolkata date '+%d %b %Y %H:%M') IST from $(hostname).
Secrets live only on the server in \`~/algo/config.json\` and are never committed.
MDEOF

cat > "$STAGE/CLAUDE.md" <<'MDEOF'
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
MDEOF

cat > "$STAGE/.github/workflows/tests.yml" <<'YMLEOF'
name: tests
on:
  push:
    branches: [main]
  pull_request:
jobs:
  test:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        python: ["3.10", "3.12"]
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: ${{ matrix.python }}
      - name: Every Python file compiles
        run: python -m compileall -q server
      - name: Unit tests (offline, no keys)
        working-directory: server
        run: |
          if ls test_*.py >/dev/null 2>&1; then
            pip install -r requirements.txt && python -m unittest discover -v
          else
            echo "no tests yet"
          fi
  no-secrets:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: No config/.env/token files or Telegram tokens in the repo
        run: |
          if git ls-files | grep -E '(^|/)(config\.json|\.env)$|token|\.bak|\.csv$' ; then echo "secret-like file committed"; exit 1; fi
          if git grep -nE '[0-9]{8,10}:[A-Za-z0-9_-]{35}' -- . ':!*.md'; then echo "Telegram-token-like text"; exit 1; fi
YMLEOF

# what was left on the server (names and sizes only, never contents)
{
  echo "# Not in git (stays on the server)"
  echo
  echo "Top-level items in ~/algo that were not copied: runtime state, secrets, journals, logs, data, backups, installers."
  echo
  ls -1A "$ALGO" | while read -r n; do
    awk -v n="$n" '$0==n||index($0,n"/")==1{f=1}END{exit !f}' "$STAGE/.files" && continue
    [ -d "$ALGO/$n" ] && echo "- \`$n/\` (folder, $(du -sh "$ALGO/$n" 2>/dev/null | cut -f1))" || echo "- \`$n\` ($(du -h "$ALGO/$n" | cut -f1))"
  done
  if [ -s "$STAGE/.redacted" ]; then echo; echo "Files where a secret value was blanked to <REDACTED> (fix: read it from config.json):"; sed 's/^/- /' "$STAGE/.redacted"; fi
} > "$STAGE/SNAPSHOT.md"
rm -f "$STAGE/.files" "$STAGE/.redacted"

# ---------- 5. commit and push ----------
if [ ! -d "$REPO/.git" ]; then git clone -q "$GH" "$REPO" 2>/dev/null || fail "could not clone $GH"; fi
cd "$REPO" || fail "no $REPO"
git remote set-url origin "$GH"
git fetch -q origin 2>/dev/null
if git rev-parse -q --verify origin/main >/dev/null; then
  BR="server-snapshot-$STAMP"; git checkout -q -B "$BR" origin/main
else
  BR=main; git symbolic-ref HEAD refs/heads/main
fi
if [ "$BR" = main ]; then                      # first snapshot: the whole repo
  find "$REPO" -mindepth 1 -maxdepth 1 ! -name .git -exec rm -rf {} +
  cp -a "$STAGE/." "$REPO/"
else                                           # later snapshots: only the server's own files; repo docs/CI stay as on main
  rm -rf "$REPO/deploy" "$REPO/SNAPSHOT.md"
  find "$REPO/server" -type f ! -name 'test_*' -delete 2>/dev/null
  cp -a "$STAGE/server" "$STAGE/deploy" "$STAGE/SNAPSHOT.md" "$REPO/"
fi
rm -rf "$STAGE"
git add -A
if git diff --cached --quiet; then echo "Nothing changed since the last snapshot."; exit 0; fi
# last guard: refuse if any forbidden file slipped in
if git diff --cached --name-only | grep -E '(^|/)(config\.json|\.env)$|token|\.csv$'; then fail "a secret-like file was staged (listed above)"; fi
git -c user.name="Nifty server" -c user.email="nifty-server@users.noreply.github.com" \
    commit -q -m "Server snapshot of ~/algo, $(TZ=Asia/Kolkata date '+%d %b %Y %H:%M') IST" || fail "commit failed"
git push -q origin "HEAD:$BR" 2>&1 | tail -3
git ls-remote --exit-code origin "refs/heads/$BR" >/dev/null || fail "push refused - did you tick 'Allow write access' on the deploy key?"
echo
echo "DONE - pushed $(git ls-files | wc -l) files to branch $BR (commit $(git rev-parse --short HEAD))"
echo "https://github.com/OhYesAbhiAbhi/tppd-nifty/tree/$BR"
cat SNAPSHOT.md | tail -n +5 | head -40
