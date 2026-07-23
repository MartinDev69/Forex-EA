# CLAUDE.md — Forex-EA operating guide

This file is auto-loaded by Claude Code. It tells you what this project is, how it
runs in production, and how to operate it safely. **This is a LIVE trading bot that
places real orders with real money.** Prefer read-only inspection; test changes on a
demo account before touching the live path; never widen risk or change execution
logic without the operator's explicit sign-off.

## What this is

An automated trading bot on **MetaTrader 5**: a Python core + FastAPI dashboard/API +
a Flutter mobile companion (`mobile/`). It reads bars from an MT5 terminal, runs
technical strategies, filters signals through an optional ML model, sizes positions by
risk, and places/manages orders through the MT5 order router.

## Current intent (2026-07 pivot): Deriv, Volatility 10 (1s) only

The bot is being pointed at **Deriv's MT5 platform** to trade a **single** synthetic
index, **`Volatility 10 (1s) Index`**, one position at a time. This is a
**configuration** posture — the engine is unchanged. Deriv's native WebSocket API is
**not** integrated; connection is pure MT5. Key `.env` values for this mode:

```
MT5_SERVER=Deriv-Demo            # prove on demo first, then DerivSVG-Server-0X
MT5_PATH=C:\Program Files\Deriv MT5\terminal64.exe
SYMBOLS=Volatility 10 (1s) Index # one symbol, no comma; must match Market Watch EXACTLY
MAX_OPEN_TRADES=1                # one trade at a time
CORRELATION_ENABLED=0            # nothing to correlate with a single symbol
USE_MT5=1
```

See `deploy/README.md` → "Running Deriv — Volatility 10 (1s) only" for the full runbook.

## Where it runs (production = this VPS)

- Repo root on the VPS: `C:\forex-ea`
- Runs 24/7 as two **NSSM Windows services**:
  - **ForexEABot** — the trading loop (`main.py`)
  - **ForexEAApi** — the FastAPI dashboard/API (uvicorn, port 8000)
- A **watchdog** runs via Task Scheduler (`scripts/watchdog.py`): restarts ForexEABot
  if the heartbeat goes stale and kills a wedged MT5 terminal if the broker stays
  disconnected. Confirm it's actually scheduled: `Get-ScheduledTask *watchdog*`.

## Operating commands (run from `C:\forex-ea`)

| Task | Command |
|------|---------|
| Service status | `.\deploy\service-control.ps1 status` |
| Restart both | `.\deploy\service-control.ps1 restart` |
| Tail bot / API stderr | `.\deploy\service-control.ps1 logs bot` / `logs api` |
| Health check | `python deploy\healthcheck.py` |
| Pull + redeploy (restarts services) | `.\deploy\update.ps1` |
| Live log tail | `Get-Content C:\forex-ea\logs\forex-ea.log -Tail 30` |

`update.ps1` refuses to pull with uncommitted local changes — run `git status` first.

## Configuration model

- `.env` (loaded by `main.py` via python-dotenv) holds all runtime config. It is
  **gitignored** and lives only on the VPS — a `git pull` never changes it. `.env.example`
  is the committed template.
- Broker credentials have two sources: the **dashboard-stored** config in
  `data/trades.db` (Fernet-encrypted with a key derived from `AUTH_SECRET`) takes
  precedence over the `.env` `MT5_*` values. `main.py` logs which source it used.
- Notable env keys: `USE_MT5` (1=real MT5, unset=mock feed), `SYMBOLS`, `TIMEFRAME`,
  `RISK_PER_TRADE`, `MAX_OPEN_TRADES`, `CORRELATION_ENABLED`, `AUTH_SECRET` (required
  for the API), `ML_MODEL_PATH` / `ML_THRESHOLD` (optional signal filter).
- State lives in `data/trades.db` (SQLite): trade journal, heartbeat, broker status,
  correlation matrix. Safe to read for diagnostics.

## Code map (`src/`)

`connection/mt5_client.py` (MT5 init/rates/account) · `strategies/` (12 technical
strategies, one instance per symbol — see `build_strategies` in `main.py`) ·
`risk/` (position sizing, portfolio heat) · `execution/` (`mt5_live.py` order router,
`journal.py`) · `bot.py` (main loop) · `watchdog/` · `api/` (FastAPI + dashboard +
`brokers.py` presets) · `econ_calendar/` (news blackout — auto-no-ops for synthetics).

## Known failure modes

- **`MT5 initialize failed: (-10005, 'IPC timeout')`** — a stale/hung `terminal64.exe`
  holds the IPC pipe. `Restart-Service` alone does NOT fix it. Kill the terminal so the
  bot relaunches a clean one: `taskkill /F /IM terminal64.exe` then
  `.\deploy\service-control.ps1 restart`. Also check for a blocking modal in the MT5 GUI
  (login failure, trial expired, update prompt).
- **No bars / no signals** on the V10 symbol — it isn't in MT5 **Market Watch**, or the
  `SYMBOLS` string doesn't match the terminal's label character-for-character. Right-click
  Market Watch → Show All and compare exactly.
- **Bot service starts then stops** — tail `logs\bot.stderr.log`; usual causes are bad
  MT5 credentials, terminal not running, or Algo Trading disabled in MT5.

## Verifying a change actually works

Don't declare a trading change done from tests alone. On `Deriv-Demo`, restart the bot
and confirm in the log: `MT5 connected … server=Deriv-Demo`, then a signal firing on
`Volatility 10 (1s) Index` and a resulting open position. Check the first trades' **lot
sizes** — a synthetic index's point value differs from forex, so `RISK_PER_TRADE` maps
to a different lot than it did on Exness.

## Repo notes

- Active branch: `hardening-and-docker`. Remote: `github.com/MartinDev69/Forex-EA`.
- Git is fast here on the VPS. (On the operator's Mac dev copy under iCloud-synced
  `~/Documents`, `git status`/`commit` hang on the 279 MB `mobile/` tree — not an issue
  on the VPS.)
