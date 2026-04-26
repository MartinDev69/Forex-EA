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
.\deploy\service-install.ps1     # registers ForexEABot + ForexEAApi services and starts them
python deploy\healthcheck.py     # verify
```

`install.ps1` creates the venv, installs requirements, creates `data/` and `logs/` directories, and copies `.env.example` to `.env` if it's missing.

`service-install.ps1` wraps `main.py` and uvicorn as Windows services via NSSM. Both are set to auto-start on boot and restart on crash (5s delay). Logs rotate at 10 MB.

## Day-to-day

| Task | Command |
|------|---------|
| Check status | `.\deploy\service-control.ps1 status` |
| Stop both | `.\deploy\service-control.ps1 stop` |
| Start both | `.\deploy\service-control.ps1 start` |
| Restart | `.\deploy\service-control.ps1 restart` |
| Tail bot stderr | `.\deploy\service-control.ps1 logs bot` |
| Tail API stderr | `.\deploy\service-control.ps1 logs api` |
| Pull + redeploy | `.\deploy\update.ps1` |
| Health check | `python deploy\healthcheck.py` |

`update.ps1` refuses to pull with uncommitted changes — stash or commit first.

## Environment variables

Controlled via `.env` (loaded by `main.py` through `python-dotenv`). The service inherits this because NSSM runs the bot from the repo root. Service-level overrides also exist:

- `USE_MT5=1` — set by `service-install.ps1` on the bot service so it uses real MT5 instead of mocks. Unset locally means the bot stays on the mock feed.
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

## Dashboard login

The API serves a single-page dashboard at `http://<vps>:8000/` — dark-themed, live-polling, with strategy toggles and an equity chart. Endpoints other than `/health` require a bearer token.

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

The training script writes both the model and a sibling `signal_filter.report.json` with accuracy, AUC, and feature importances. Restart the bot service to pick up the new model:

```powershell
.\deploy\service-control.ps1 restart
```

### Typical refresh loop

1. `scripts\fetch_bars.py` — pull the latest bars (runs in minutes, idempotent).
2. `scripts\backtest.py --symbol EURUSD --strategy all` — sanity-check strategy PnL on the current data.
3. `scripts\train_signal_filter.py` — retrain on the updated journal + bars.
4. `service-control.ps1 restart` — swap the live model.

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

**Bot service starts then stops immediately** — tail `logs\bot.stderr.log`. Typical causes: bad MT5 credentials in `.env`, MT5 terminal not running, Algo Trading disabled in MT5.

**`MetaTrader5` import fails on install** — check that the venv uses Python 3.12, not 3.13. Recreate the venv if it was made with the wrong version.

**API not reachable by mobile app** — Windows Firewall may be blocking 8000. Open it:
```powershell
New-NetFirewallRule -DisplayName "ForexEA API" -Direction Inbound -Protocol TCP -LocalPort 8000 -Action Allow
```

**Journal grows unbounded** — it's just SQLite; safe to prune with `DELETE FROM trades WHERE closed_at < date('now','-90 day')`, then `VACUUM`.
