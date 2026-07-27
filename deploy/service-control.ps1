# Start/stop/status helpers for the Forex-EA runtime.
#
# The runtime is deliberately SPLIT across two mechanisms (see CLAUDE.md
# "Where it runs"), because MT5 only initialises reliably with a real desktop
# session -- from session 0 it returns -10005 IPC timeout:
#
#   bot       -> scheduled task ForexEA-Bot, interactive session, autologon user
#   api       -> NSSM service ForexEAApi, session 0 (no MT5, so no constraint)
#   watchdog  -> scheduled task ForexEA-Watchdog, SYSTEM, every 60s
#
#   .\deploy\service-control.ps1 status
#   .\deploy\service-control.ps1 restart          # bot + api
#   .\deploy\service-control.ps1 restart bot
#   .\deploy\service-control.ps1 logs bot         # tail logs\bot-task.log
#   .\deploy\service-control.ps1 logs api         # tail logs\api.stderr.log

param(
    # Position 0 is explicit: without it PowerShell binds the first positional
    # argument to -Target instead, and `service-control.ps1 status` fails.
    [Parameter(Mandatory = $true, Position = 0)]
    [ValidateSet("start", "stop", "restart", "status", "logs")]
    [string]$Action,

    [Parameter(Position = 1)]
    [ValidateSet("bot", "api", "watchdog", "all")]
    [string]$Target = "all"
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
$logsDir  = Join-Path $RepoRoot "logs"

$BotTask      = "ForexEA-Bot"
$WatchdogTask = "ForexEA-Watchdog"
$ApiService   = "ForexEAApi"

# Which log each target's output actually lands in. Note bot-task.log, NOT the
# stale bot.stderr.log left behind by the retired NSSM bot service.
$LogFiles = @{
    bot      = "bot-task.log"
    api      = "api.stderr.log"
    watchdog = "watchdog.log"
}

function Stop-BotTask {
    Write-Host "Stopping task $BotTask"
    Stop-ScheduledTask -TaskName $BotTask -ErrorAction SilentlyContinue
}

function Start-BotTask {
    Write-Host "Starting task $BotTask"
    Start-ScheduledTask -TaskName $BotTask

    # Starting an *interactive* task from an interactive shell is unreliable:
    # the spawned process lands in the calling session and Windows tears it down
    # with CONTROL_C_EXIT (0xC000013A) when the calling shell exits. Observed
    # 2026-07-26 -- every shell-started instance wrote exactly ONE heartbeat and
    # died, while every watchdog-started one (SYSTEM, own session) ran for
    # hundreds of ticks. Verify rather than reporting a false success: the
    # heartbeat must still be advancing a few seconds later.
    Start-Sleep -Seconds 12
    $py = Join-Path $RepoRoot "venv\Scripts\python.exe"
    $probeScript = Join-Path $PSScriptRoot "heartbeat_probe.py"
    $db = Join-Path $RepoRoot "data\trades.db"
    if ((Test-Path $py) -and (Test-Path $probeScript) -and (Test-Path $db)) {
        $first = (& $py $probeScript $db 2>$null) -split ' ' | Select-Object -First 1
        Start-Sleep -Seconds 20
        $second = (& $py $probeScript $db 2>$null) -split ' ' | Select-Object -First 1
        if ($first -eq $second) {
            Write-Warning "$BotTask is NOT ticking (heartbeat stuck at $first)."
            Write-Warning "A shell-started interactive task often dies with the shell. The"
            Write-Warning "watchdog will relaunch it correctly within WATCHDOG_COOLDOWN_S"
            Write-Warning "(default 600s), or start it from an elevated non-interactive"
            Write-Warning "context: schtasks /run /tn $BotTask"
        } else {
            Write-Host "$BotTask is ticking ($first -> $second)"
        }
    }
}

function Restart-BotTask {
    Stop-BotTask
    # Give main.py a moment to release the MT5 IPC pipe before the new instance
    # initialises against it, otherwise the fresh start can hit -10005.
    Start-Sleep -Seconds 3
    Start-BotTask
}

switch ($Action) {

    "status" {
        Write-Host "`n-- Scheduled tasks --"
        Get-ScheduledTask -TaskName $BotTask, $WatchdogTask -ErrorAction SilentlyContinue |
            Select-Object TaskName, State,
                @{n = "RunAs";   e = { $_.Principal.UserId } },
                @{n = "Session"; e = { $_.Principal.LogonType } } |
            Format-Table -AutoSize

        Get-ScheduledTaskInfo -TaskName $BotTask -ErrorAction SilentlyContinue |
            Select-Object @{n = "Task"; e = { $BotTask } }, LastRunTime, LastTaskResult |
            Format-Table -AutoSize

        Write-Host "-- Service --"
        Get-Service $ApiService -ErrorAction SilentlyContinue |
            Select-Object Name, Status, StartType | Format-Table -AutoSize

        # The task merely being "Running" only proves the .cmd wrapper is up.
        # The heartbeat in the DB is what the watchdog trusts, so surface it too.
        Write-Host "-- Heartbeat (data\trades.db) --"
        $venvPython = Join-Path $RepoRoot "venv\Scripts\python.exe"
        $db = Join-Path $RepoRoot "data\trades.db"
        if ((Test-Path $venvPython) -and (Test-Path $db)) {
            $probe = @"
import sqlite3, datetime
c = sqlite3.connect(r'$db')
now = datetime.datetime.now(datetime.timezone.utc)
for label, sql in (
    ('bot heartbeat', "select last_tick_at, tick_count, pid from watchdog_heartbeat where process_name='bot'"),
    ('broker status', 'select updated_at, connected, server from broker_status limit 1'),
):
    row = c.execute(sql).fetchone()
    if not row:
        print(f'  {label}: (none)')
        continue
    age = (now - datetime.datetime.fromisoformat(row[0])).total_seconds()
    print(f'  {label}: {age:7.1f}s ago  {row[1:]}')
"@
            $probe | & $venvPython - 2>&1 | Write-Host
        } else {
            Write-Host "  (venv or data\trades.db missing -- skipped)"
        }

        # Loud warning if someone re-enabled the retired bot service: two live
        # main.py instances would double-trade the same account.
        $stale = Get-Service "ForexEABot" -ErrorAction SilentlyContinue
        if ($stale -and $stale.Status -eq "Running") {
            Write-Warning "Retired NSSM service ForexEABot is RUNNING -- that is a second main.py against the same account. Stop and disable it."
        }
    }

    "start" {
        if ($Target -in @("bot", "all"))      { Start-BotTask }
        if ($Target -in @("api", "all"))      { Write-Host "Starting $ApiService";  Start-Service $ApiService }
        if ($Target -in @("watchdog", "all")) { Enable-ScheduledTask -TaskName $WatchdogTask | Out-Null }
    }

    "stop" {
        # Bot first, so it stops placing orders while the API is still up to observe.
        if ($Target -in @("bot", "all"))      { Stop-BotTask }
        if ($Target -in @("api", "all"))      { Write-Host "Stopping $ApiService";  Stop-Service $ApiService -Force }
        if ($Target -in @("watchdog", "all")) { Disable-ScheduledTask -TaskName $WatchdogTask | Out-Null }
    }

    "restart" {
        # Watchdog is intentionally left alone -- it is stateless and its whole
        # job is to notice if this restart fails to bring the bot back.
        if ($Target -in @("bot", "all")) { Restart-BotTask }
        if ($Target -in @("api", "all")) { Write-Host "Restarting $ApiService"; Restart-Service $ApiService }
    }

    "logs" {
        if ($Target -eq "all") { throw "logs requires a target: bot, api, or watchdog" }
        $file = Join-Path $logsDir $LogFiles[$Target]
        if (-not (Test-Path $file)) { throw "log file not found: $file" }
        Get-Content -Path $file -Wait -Tail 50
    }
}
