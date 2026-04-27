# Register the watchdog as a Windows scheduled task.
#
# Runs scripts/watchdog.py every 60s under SYSTEM, so it has rights to
# Restart-Service and taskkill MT5 even if the operator user is logged out.
#
# Idempotent -- re-running replaces the existing task.

param(
    [int]$IntervalSeconds = 60,
    [string]$TaskName = "ForexEA-Watchdog"
)

$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

$venvPython = Join-Path $RepoRoot "venv\Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    throw "venv missing at $venvPython -- run .\deploy\install.ps1 first."
}

$watchdogScript = Join-Path $RepoRoot "scripts\watchdog.py"
if (-not (Test-Path $watchdogScript)) {
    throw "Watchdog script missing at $watchdogScript."
}

$logsDir = Join-Path $RepoRoot "logs"
if (-not (Test-Path $logsDir)) { New-Item -ItemType Directory -Path $logsDir | Out-Null }

# Wrap the python call in cmd /c so we can redirect stdout/stderr to a log
# file. The log gets one line per tick -- easy to tail with service-control.
$logFile = Join-Path $logsDir "watchdog.log"
$cmd = "/c `"$venvPython`" `"$watchdogScript`" >> `"$logFile`" 2>&1"

# --- Build the task -------------------------------------------------------
$action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument $cmd -WorkingDirectory $RepoRoot

# Repetition every $IntervalSeconds, indefinitely. PowerShell's
# RepetitionInterval needs a TimeSpan >= 1 minute on most builds, so we
# clamp 60s as the floor.
$intervalSpan = if ($IntervalSeconds -lt 60) { New-TimeSpan -Seconds 60 } else { New-TimeSpan -Seconds $IntervalSeconds }
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date)
$trigger.Repetition = (New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval $intervalSpan -RepetitionDuration (New-TimeSpan -Days 365)).Repetition

$principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 5) `
    -MultipleInstances IgnoreNew

# Replace any existing task with the same name.
$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "Removing existing task $TaskName"
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false | Out-Null
}

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Principal $principal -Settings $settings `
    -Description "Forex-EA self-healing watchdog. Restarts ForexEABot if its heartbeat goes stale, kills MT5 if broker stays disconnected." | Out-Null

Write-Host "Registered scheduled task '$TaskName' (every ${IntervalSeconds}s as SYSTEM)."
Write-Host "Tail log:  Get-Content $logFile -Tail 50 -Wait"
Write-Host "Disable:   Disable-ScheduledTask -TaskName $TaskName"
Write-Host "Remove:    Unregister-ScheduledTask -TaskName $TaskName -Confirm:`$false"
