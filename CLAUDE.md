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
- Public dashboard/API: **`http://141.11.232.239:8000`**. (The old `163.5.178.251` is
  dead — if you find it anywhere, it's stale.)
- The three pieces start **three different ways**. This split is deliberate; don't
  "unify" it back into services:

| Piece | Starts via | Runs as | Logs |
|-------|-----------|---------|------|
| **Trading loop** (`main.py`) | scheduled task **`ForexEA-Bot`** → `deploy\run-bot.cmd` | `Administrator`, **Interactive**, at-logon trigger | `logs\bot-task.log` + `logs\forex-ea.log` |
| **API / dashboard** (uvicorn :8000) | NSSM service **`ForexEAApi`** | `LocalSystem`, session 0 | `logs\api.stdout.log` + `logs\api.stderr.log` |
| **Watchdog** (`scripts/watchdog.py`) | scheduled task **`ForexEA-Watchdog`** → `deploy\run-watchdog.cmd`, every 60s | `SYSTEM` | `logs\watchdog.log` + `watchdog_actions` table |

**Why the bot is a task and not a service.** MT5 only initialises reliably when there's
a real desktop session; from session 0 it fails with `-10005 IPC timeout`. So the
trading loop runs inside the interactive session of an auto-logged-on user. The API has
no such constraint, so it stayed an NSSM service.

**Autologon wiring.** `Autologon.exe` (Sysinternals) set `AutoAdminLogon=1` /
`DefaultUserName=Administrator` / `DefaultDomainName=VPS35079963` under
`HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon`, with the password stored
as an **LSA secret** (not plaintext in the registry — don't "fix" this by adding a
`DefaultPassword` value). Boot → Administrator logs in automatically → the at-logon
trigger fires `ForexEA-Bot` → MT5 and `main.py` come up in that session. **If autologon
ever breaks, the box boots to the lock screen and the bot never starts** — while the API
still does, so a green dashboard is not proof the bot is running.

The bot task self-heals: `RestartCount=999`, `RestartInterval=1 min`, execution time
limit disabled (runs forever).

**Vestigial:** the old NSSM service `ForexEABot` still exists but is **Stopped +
Disabled**. Leave it that way — starting it would run a second `main.py` against the
same account.

## Operating commands (run from `C:\forex-ea`)

| Task | Command |
|------|---------|
| Status of all three | `.\deploy\service-control.ps1 status` |
| Restart bot + API | `.\deploy\service-control.ps1 restart` |
| Restart just the bot | `.\deploy\service-control.ps1 restart bot` |
| Tail bot / API logs | `.\deploy\service-control.ps1 logs bot` / `logs api` |
| Health check | `python deploy\healthcheck.py` |
| Pull + redeploy | `.\deploy\update.ps1` |

Raw equivalents, if you'd rather not use the wrapper:

```powershell
Get-ScheduledTask   ForexEA-Bot, ForexEA-Watchdog          # task state
Get-ScheduledTaskInfo ForexEA-Bot                          # LastRunTime / LastTaskResult
Get-Service ForexEAApi                                     # API service
Stop-ScheduledTask -TaskName ForexEA-Bot; Start-ScheduledTask -TaskName ForexEA-Bot
Restart-Service ForexEAApi
```

**Is the bot actually alive?** The task showing `Running` only means the `.cmd` wrapper
is up. The authoritative signal is the DB heartbeat the watchdog reads —
`watchdog_heartbeat.last_tick_at` (and `broker_status.updated_at`) in `data/trades.db`
should be seconds old, with `watchdog_actions` logging `all healthy`. `status` prints
these. Since 2026-07-25 `logs\forex-ea.log` also carries loop-level records (`src.bot`,
`src.execution.mt5_live`), so a live tail is now a real liveness signal too — before that
fix the file only ever received `main.py`'s startup banner.

`update.ps1` refuses to pull with uncommitted local changes — run `git status` first.

## Configuration model

- `.env` (loaded by `main.py` via python-dotenv) holds all runtime config. It is
  **gitignored** and lives only on the VPS — a `git pull` never changes it. `.env.example`
  is the committed template.
- Broker credentials come from **`.env` only** (`MT5_LOGIN` / `MT5_PASSWORD` /
  `MT5_SERVER` / `MT5_PATH`). The dashboard can also store a Fernet-encrypted config in
  `data/trades.db`, and this file used to claim that took precedence — it never did.
  `main.py` called `get_decrypted()` without the required `username` argument and a bare
  `except` swallowed the `TypeError`, so every start silently fell through to `.env`.
  As of 2026-07-25 the dead branch is gone and `main.py` logs a warning if a dashboard
  config exists while being ignored. Wiring it up needs a designated bot-owner user in
  the API — per-user rows give an unattended process no correct row to pick.
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

- **`MT5 initialize failed: (-10005, 'IPC timeout')`** — either a stale/hung
  `terminal64.exe` is holding the IPC pipe, or the bot is running without an interactive
  desktop session. Restarting alone does NOT fix the first case; kill the terminal so the
  bot relaunches a clean one: `taskkill /F /IM terminal64.exe` then
  `.\deploy\service-control.ps1 restart bot`. Also check for a blocking modal in the MT5
  GUI (login failure, trial expired, update prompt). If it's persistent and reproducible
  from a clean start, confirm autologon still works and that `ForexEA-Bot`'s principal is
  still `Administrator` / **Interactive** — a task flipped to "Run whether user is logged
  on or not" lands in session 0 and will IPC-timeout forever.
- **Signals fire but nothing ever fills — `retcode=10027 AutoTrading disabled by client`.**
  The terminal's Algo Trading toggle is off. *Everything else looks perfectly healthy*:
  heartbeat ticking, broker connected, strategies evaluating — the bot just silently
  places nothing. This cost ~32h of trading on 2026-07-24..26. `main.py` now logs
  `AutoTrading enabled in terminal (trade_allowed=True)` at startup, or a loud warning if
  not, so check that line first. **Fix it in the GUI**: focus the terminal and press
  **Ctrl+E** (the Algo Trading button must be green), then restart the bot. Editing
  `[Experts] Enabled=1` in `config\common.ini` does *not* hold — a running terminal
  flushes its in-memory state over that file within minutes. See `deploy/README.md`.
- **No bars / no signals** on the V10 symbol — it isn't in MT5 **Market Watch**, or the
  `SYMBOLS` string doesn't match the terminal's label character-for-character. Right-click
  Market Watch → Show All and compare exactly.
- **Bot starts then stops** — tail `logs\bot-task.log` (the task's own stdout/stderr;
  `logs\bot.stderr.log` is from the retired NSSM service and is frozen in the past).
  Usual causes are bad MT5 credentials, terminal not running, or Algo Trading disabled.
- **Bot never starts after a reboot** — the box booted to a lock screen instead of
  auto-logging in, so the at-logon trigger never fired. Check `AutoAdminLogon` under
  `Winlogon` and re-run `deploy\Autologon.exe`. The API will look fine throughout.

## Verifying a change actually works

Don't declare a trading change done from tests alone. On `Deriv-Demo`, restart the bot
(`.\deploy\service-control.ps1 restart bot`) and confirm in `logs\bot-task.log`:
`MT5 connected … server=Deriv-Demo`, then a signal firing on `Volatility 10 (1s) Index`
and a resulting open position, cross-checked against the `trades` table in
`data/trades.db`. Check the first
trades' **lot sizes** — a synthetic index's point value differs from forex, so
`RISK_PER_TRADE` maps to a different lot than it did on Exness.

## Repo notes

- Active branch: `hardening-and-docker`. Remote: `github.com/MartinDev69/Forex-EA`.
- Git is fast here on the VPS. (On the operator's Mac dev copy under iCloud-synced
  `~/Documents`, `git status`/`commit` hang on the 279 MB `mobile/` tree — not an issue
  on the VPS.)
