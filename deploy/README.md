# Windows VPS deployment

Short runbook for getting Forex-EA running 24/7 on a Windows VPS.

## Prerequisites

- Windows Server or Windows 10/11 VPS
- Python 3.12 on PATH (MetaTrader5 wheels don't support 3.13 yet)
- MetaTrader 5 terminal installed, with a demo or live account that has **Algo Trading** enabled
- Git (`winget install --id Git.Git`)
- NSSM (`choco install nssm`, or manual from https://nssm.cc)

## First-time install

```powershell
git clone <repo-url> C:\forex-ea
cd C:\forex-ea
.\deploy\install.ps1
notepad .env                     # fill in MT5 credentials, symbols, Telegram, etc.
.\deploy\service-install.ps1     # registers the ForexEAApi service (see caveat below)
.\deploy\watchdog-install.ps1    # registers the ForexEA-Watchdog scheduled task
python deploy\healthcheck.py     # verify
```

`install.ps1` creates the venv, installs requirements, creates `data/` and `logs/` directories, and copies `.env.example` to `.env` if it's missing.

> **Caveat:** `service-install.ps1` predates the current run model and still registers a
> `ForexEABot` NSSM service alongside `ForexEAApi`. **The bot must not run as a service**
> — see "Durable runtime" below. After running it, stop and disable `ForexEABot`
> (`Stop-Service ForexEABot; Set-Service ForexEABot -StartupType Disabled`) and set up
> the interactive task instead. Only the `ForexEAApi` half of that script is still correct.

## Durable runtime

The runtime is split across two mechanisms on purpose:

| Piece | Mechanism | Runs as | Wrapper | Log |
|-------|-----------|---------|---------|-----|
| Trading loop (`main.py`) | scheduled task `ForexEA-Bot`, at-logon trigger | `Administrator`, **Interactive** | `deploy\run-bot.cmd` | `logs\bot-task.log` |
| API / dashboard (uvicorn :8000) | NSSM service `ForexEAApi` | `LocalSystem` (session 0) | — | `logs\api.stderr.log` |
| Watchdog | scheduled task `ForexEA-Watchdog`, every 60s | `SYSTEM` | `deploy\run-watchdog.cmd` | `logs\watchdog.log` |

**Why the bot can't be a service.** MT5 only initialises reliably when a real desktop
session exists. Launched from session 0 — which is where every Windows service lives —
`mt5.initialize()` fails with `(-10005, 'IPC timeout')` indefinitely. So the trading loop
runs as an *interactive* scheduled task in the session of an auto-logged-on user. The API
touches no MT5 and stays a service.

The bot task is configured to restart itself on exit: `RestartCount=999`,
`RestartInterval=1 min`, execution time limit disabled.

**Autologon.** So that a desktop session exists unattended after every reboot, Sysinternals
[Autologon](https://learn.microsoft.com/sysinternals/downloads/autologon) sets
`AutoAdminLogon=1` and `DefaultUserName` under
`HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon`, storing the password as an
**LSA secret** rather than a plaintext `DefaultPassword` registry value. Run it once per
rebuild:

```powershell
.\deploy\Autologon.exe            # not in git -- see .gitignore; download from the link above
```

Boot → user auto-logs-in → at-logon trigger fires `ForexEA-Bot` → MT5 + `main.py` come up
in that session. **If autologon breaks, the VPS sits at the lock screen and the bot never
starts, while the API keeps serving happily** — so a reachable dashboard is not evidence
the bot is alive.

Registering the bot task from scratch:

```powershell
$action    = New-ScheduledTaskAction -Execute "C:\forex-ea\deploy\run-bot.cmd"
$trigger   = New-ScheduledTaskTrigger -AtLogOn -User "Administrator"
$principal = New-ScheduledTaskPrincipal -UserId "Administrator" -LogonType Interactive -RunLevel Highest
$settings  = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew `
                -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
                -ExecutionTimeLimit (New-TimeSpan -Seconds 0) -AllowStartIfOnBatteries
Register-ScheduledTask -TaskName "ForexEA-Bot" -Action $action -Trigger $trigger `
    -Principal $principal -Settings $settings -Force
```

`-LogonType Interactive` is the load-bearing flag. Flipping the task to "Run whether user
is logged on or not" puts it back in session 0 and MT5 will IPC-timeout forever.

## Day-to-day

| Task | Command |
|------|---------|
| Check status (all three) | `.\deploy\service-control.ps1 status` |
| Stop bot + API | `.\deploy\service-control.ps1 stop` |
| Start bot + API | `.\deploy\service-control.ps1 start` |
| Restart bot + API | `.\deploy\service-control.ps1 restart` |
| Restart just the bot | `.\deploy\service-control.ps1 restart bot` |
| Tail bot log | `.\deploy\service-control.ps1 logs bot` |
| Tail API stderr | `.\deploy\service-control.ps1 logs api` |
| Tail watchdog log | `.\deploy\service-control.ps1 logs watchdog` |
| Pull + redeploy | `.\deploy\update.ps1` |
| Health check | `python deploy\healthcheck.py` |

`update.ps1` refuses to pull with uncommitted changes — stash or commit first.

**Confirming the bot is really alive.** `ForexEA-Bot` showing `Running` only proves the
`.cmd` wrapper is up. The authoritative signal is the heartbeat in `data/trades.db` —
`watchdog_heartbeat.last_tick_at` and `broker_status.updated_at` should both be seconds
old, and `watchdog_actions` should be logging `all healthy`. `status` above prints these.
`logs\forex-ea.log` can stay unwritten for long stretches on a healthy bot, so log silence
alone does not mean it's down.

## Environment variables

Controlled via `.env` (loaded by `main.py` through `python-dotenv`). The bot picks it up because `run-bot.cmd` does `cd /d C:\forex-ea` before launching, so the repo root is the working directory. Overrides also exist:

- `USE_MT5=1` — must be set in `.env` so the bot uses real MT5 instead of mocks. Unset means the bot stays on the mock feed. (Older docs said `service-install.ps1` set this on the bot service; that path is retired — `.env` is now the only source.)
- `ML_MODEL_PATH` — XGBoost model path. If missing, the filter is skipped.
- `ML_THRESHOLD` — minimum P(win) to take a trade.
- `AUTH_SECRET` — **required** for the API. 32+ char random string used to sign JWTs. If missing, the API refuses to issue or accept tokens (fail-closed). Generate with:
  ```powershell
  python -c "import secrets; print(secrets.token_urlsafe(48))"
  ```
  Save to `.env` as `AUTH_SECRET=...`. Rotating invalidates every outstanding session, so users must sign in again.

## Broker configuration

The dashboard's **MT5 broker** card lets you pick from a preset list (Exness, XM, Deriv-MT5, IC Markets, FBS, Pepperstone, or Custom) and enter your MT5 login / password / server. Credentials are encrypted with Fernet (AES-128-CBC + HMAC) using a key derived from `AUTH_SECRET` via PBKDF2-HMAC-SHA256 — so they live in `data/trades.db` at rest but cannot be read without the secret.

**Testing a connection** (`POST /broker/test`) opens a temporary MT5 session in the API process, fetches `account_info`, and disconnects. On macOS/Linux the `MetaTrader5` wheel doesn't exist, so the test reports `ok=false` — meaning you can *save* creds from a Mac but must *verify* on the Windows VPS.

**Priority at startup:** `main.py` reads the DB-stored config first; if missing or undecryptable, it falls back to `MT5_LOGIN`/`MT5_PASSWORD`/`MT5_SERVER` from `.env`. The bot writes its own connection status back to `broker_status` every start, so the dashboard's status badge reflects reality without needing IPC.

> **Rotating `AUTH_SECRET` invalidates saved broker passwords** (as well as all JWT sessions). If you rotate, clear the broker config via the dashboard and re-enter.

For Deriv users: the presets target Deriv's **MT5** accounts. The native Deriv WebSocket API (synthetic indices etc.) is a separate adapter and not wired in.

## Running Deriv — Volatility 10 (1s) only

This deployment is configured to trade **one** market: `Volatility 10 (1s) Index` on Deriv's MT5 platform. Nothing about the engine changes — signals, execution, risk, and the watchdog all run as-is, just pointed at a single synthetic index.

**One-time setup on the VPS:**

1. Open a **Deriv** account (https://deriv.com) and create an MT5 **Derived** account — the type that carries synthetic indices. Note its login, password, and server.
2. Install the **Deriv MT5** terminal (`C:\Program Files\Deriv MT5\terminal64.exe`).
3. Launch it, log into the Derived account, and enable **Algo Trading** (the toolbar button must be green).
4. In **Market Watch**: right-click → *Show All*, then confirm the symbol is listed as **`Volatility 10 (1s) Index`**. The bot cannot fetch bars for a symbol that isn't in Market Watch, and the label must match `.env` exactly.

**`.env` (demo first, then real):**

```ini
MT5_LOGIN=<your Deriv login>
MT5_PASSWORD=<your Deriv password>
MT5_SERVER=Deriv-Demo                 # prove it works here first…
MT5_PATH=C:\Program Files\Deriv MT5\terminal64.exe
USE_MT5=1

SYMBOLS=Volatility 10 (1s) Index      # exactly one symbol — no comma
MAX_OPEN_TRADES=1                     # one trade at a time
CORRELATION_ENABLED=0                 # no cross-pair heat to throttle
```

When demo is proven, change **only** `MT5_SERVER` to the real one (`DerivSVG-Server`, `DerivSVG-Server-02`, or `DerivSVG-Server-03` — your account panel shows which) and restart the bot.

**Verify the switch (signals + execution):**

```powershell
.\deploy\service-control.ps1 restart bot
Get-Content C:\forex-ea\logs\bot-task.log -Tail 30    # look for: MT5 connected … server=Deriv-Demo
```

Then watch for a signal firing on `Volatility 10 (1s) Index` and a resulting open position. On demo, check the **first few trades' lot sizes** — a synthetic index has a very different point value from forex, so `RISK_PER_TRADE=0.01` maps to a different lot than it did on Exness.

**Backfilling bars for the ML filter** uses the same symbol string (quote it — it has spaces):

```powershell
venv\Scripts\python scripts\fetch_bars.py --symbols "Volatility 10 (1s) Index" --timeframe M15
```

## Dashboard login

The API serves a single-page dashboard at `http://<vps>:8000/` — currently **http://141.11.232.239:8000/** — dark-themed, live-polling, with strategy toggles and an equity chart. Endpoints other than `/health` require a bearer token.

> The public IP is baked into a few committed files (`src/api/server.py` setup links, `mt5/AntiGreedCopier.mq5`, `mobile/lib/api/config.dart`, `mobile/ios/Runner/Info.plist`) and into `PUBLIC_BASE_URL` in `.env`. If the VPS IP changes again, grep for the old one and update all of them together.

Seed the first admin user:

```powershell
venv\Scripts\python scripts\create_user.py --username mac
# prompts for password (min 12 chars, confirmed twice)
```

Reset a forgotten password:

```powershell
venv\Scripts\python scripts\create_user.py --username mac --reset
```

Security posture:

- Passwords hashed with bcrypt (passlib default cost).
- Sessions are stateless HS256 JWTs, 1h TTL, signed with `AUTH_SECRET`.
- Login is rate-limited: 5 attempts per 15 min per client IP (sliding window).
- Wrong username runs a dummy hash so response timing doesn't leak user existence.
- All state-mutating endpoints (`/bot/*`, `/strategies/*/toggle`) require a valid token.

## Backfilling bar history

The ML trainer needs a bar cache under `data/bars/`. Populate it from MT5:

```powershell
venv\Scripts\python scripts\fetch_bars.py --symbols EURUSD,GBPUSD --timeframe M15
venv\Scripts\python scripts\fetch_bars.py --symbols EURUSD --timeframe M15 --since 2022-01-01
```

Rerunning is idempotent — the ingester resumes from the last stored bar. First-run cold start defaults to 90 days; pass `--since` for a longer window. Parquet is used when pyarrow is available, otherwise CSV. The same files feed the training script and the backtester.

## Training the ML filter

Once the bar cache is populated and `data/trades.db` has some closed trades:

```powershell
venv\Scripts\python scripts\train_signal_filter.py `
    --db data\trades.db `
    --bars-dir data\bars `
    --out data\models\signal_filter.json
```

The training script writes both the model and a sibling `signal_filter.report.json` with accuracy, AUC, and feature importances. Restart the bot to pick up the new model:

```powershell
.\deploy\service-control.ps1 restart bot
```

### Typical refresh loop

1. `scripts\fetch_bars.py` — pull the latest bars (runs in minutes, idempotent).
2. `scripts\backtest.py --symbol EURUSD --strategy all` — sanity-check strategy PnL on the current data.
3. `scripts\train_signal_filter.py` — retrain on the updated journal + bars.
4. `service-control.ps1 restart bot` — swap the live model.

## Backtesting a strategy

```powershell
venv\Scripts\python scripts\backtest.py --symbol EURUSD --strategy ma_crossover
venv\Scripts\python scripts\backtest.py --symbol EURUSD --strategy all `
    --since 2023-01-01 --out data\backtests
```

Reports write to `data/backtests/{symbol}_{strategy}_{timestamp}.json` plus a
sibling `.equity.csv`. Compare runs with `diff` or drop them into a notebook
to chart equity curves side-by-side.

## Monitoring

`deploy\healthcheck.py` exits non-zero if any check fails. For periodic monitoring:

```powershell
schtasks /Create /TN "ForexEA Health" /TR "C:\forex-ea\venv\Scripts\python.exe C:\forex-ea\deploy\healthcheck.py --json" /SC MINUTE /MO 5
```

Pipe the JSON output to whatever alerting you prefer (Telegram bot, email, Prometheus pushgateway).

## Common issues

**`nssm not found`** — install it (`choco install nssm`) or put the directory on PATH.

**Bot starts then stops immediately** — tail `logs\bot-task.log`. Typical causes: bad MT5 credentials in `.env`, MT5 terminal not running, Algo Trading disabled in MT5. (`logs\bot.stderr.log` belongs to the retired NSSM bot service and no longer updates — don't read it.)

**Bot never came back after a reboot** — the VPS booted to the lock screen, so no interactive session existed and the at-logon trigger never fired. Verify autologon and re-run `deploy\Autologon.exe`:
```powershell
Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon' |
    Select-Object AutoAdminLogon, DefaultUserName
```
The API stays up throughout this failure, so the dashboard being reachable proves nothing.

**`MT5 initialize failed: (-10005, 'IPC timeout')`** — usually a stale/hung `terminal64.exe` holding the IPC pipe, so a fresh init can't connect. Restarting alone does **not** fix it — kill the terminal so the bot relaunches a clean one:
```powershell
taskkill /F /IM terminal64.exe
.\deploy\service-control.ps1 restart bot
```
If it recurs, check for a modal dialog blocking the terminal (login failure, "trial expired", update prompt) and confirm the watchdog task is running (`Get-ScheduledTask *watchdog*`) — its job is to recycle a wedged terminal automatically. If it's constant from a clean start, confirm `ForexEA-Bot`'s principal is still `Administrator` / `Interactive`; a task moved to session 0 IPC-timeouts forever.

**No bars / no signals on `Volatility 10 (1s) Index`** — the symbol isn't in Market Watch, or the `SYMBOLS` string doesn't match the terminal's label character-for-character. Right-click Market Watch → *Show All* and compare exactly.

**`MetaTrader5` import fails on install** — check that the venv uses Python 3.12, not 3.13. Recreate the venv if it was made with the wrong version.

**API not reachable by mobile app** — Windows Firewall may be blocking 8000. Open it:
```powershell
New-NetFirewallRule -DisplayName "ForexEA API" -Direction Inbound -Protocol TCP -LocalPort 8000 -Action Allow
```

**Journal grows unbounded** — it's just SQLite; safe to prune with `DELETE FROM trades WHERE closed_at < date('now','-90 day')`, then `VACUUM`.
