# Register the API as a Windows service using NSSM.
#
# Prerequisites:
#   * NSSM installed and on PATH (https://nssm.cc -- chocolatey: `choco install nssm`).
#   * .\deploy\install.ps1 has been run (venv exists).
#
# Service created:
#   ForexEAApi -- uvicorn serving src.api.server:app on port 8000.
#
# The BOT is deliberately NOT installed here. MT5 only initialises reliably with
# a real desktop session; from session 0 -- where every Windows service runs --
# mt5.initialize() returns (-10005, 'IPC timeout') forever. The trading loop is
# an interactive scheduled task instead: run .\deploy\bot-task-install.ps1.
# See CLAUDE.md "Where it runs" and deploy/README.md "Durable runtime".
#
# This script actively removes a legacy ForexEABot service if it finds one --
# leaving it installed risks a second main.py trading the same account.
#
# Runs as the current user by default. For production, use
# `nssm set <svc> ObjectName <user> <pass>` to run under a dedicated service account.

$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

# --- NSSM sanity check -----------------------------------------------------
$nssm = Get-Command nssm -ErrorAction SilentlyContinue
if (-not $nssm) {
    throw "nssm not found on PATH. Install with `choco install nssm` or download from nssm.cc."
}

$venvPython = Join-Path $RepoRoot "venv\Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    throw "venv missing at $venvPython -- run .\deploy\install.ps1 first."
}

$logsDir = Join-Path $RepoRoot "logs"
if (-not (Test-Path $logsDir)) { New-Item -ItemType Directory -Path $logsDir | Out-Null }

function Install-Svc {
    param(
        [string]$Name,
        [string]$Exe,
        [string]$AppArgs,
        [string]$Cwd,
        [string]$StdoutLog,
        [string]$StderrLog
    )

    $existing = Get-Service -Name $Name -ErrorAction SilentlyContinue
    if ($existing) {
        Write-Host "Service $Name already exists -- stopping and reconfiguring"
        & nssm stop $Name confirm | Out-Null
        & nssm remove $Name confirm | Out-Null
    }

    & nssm install $Name $Exe | Out-Host
    & nssm set $Name AppParameters $AppArgs | Out-Null
    & nssm set $Name AppDirectory $Cwd | Out-Null
    & nssm set $Name AppStdout $StdoutLog | Out-Null
    & nssm set $Name AppStderr $StderrLog | Out-Null
    & nssm set $Name AppRotateFiles 1 | Out-Null
    & nssm set $Name AppRotateBytes 10485760 | Out-Null  # 10 MB
    & nssm set $Name Start SERVICE_AUTO_START | Out-Null
    & nssm set $Name AppRestartDelay 5000 | Out-Null
    Write-Host "$Name configured"
}

# --- Remove any legacy bot service ------------------------------------------
# Older revisions of this script installed the bot as a session-0 NSSM service.
# That can never work (IPC timeout) and would double-trade if it ever did, so
# tear it down rather than leave it for someone to "helpfully" start.
$legacyBot = Get-Service -Name "ForexEABot" -ErrorAction SilentlyContinue
if ($legacyBot) {
    Write-Warning "Removing legacy ForexEABot service -- the bot runs as the ForexEA-Bot scheduled task now."
    & nssm stop ForexEABot confirm | Out-Null
    & nssm remove ForexEABot confirm | Out-Null
}

# --- API -------------------------------------------------------------------
$uvicornExe = Join-Path $RepoRoot "venv\Scripts\uvicorn.exe"
Install-Svc `
    -Name "ForexEAApi" `
    -Exe $uvicornExe `
    -AppArgs "src.api.server:app --host 0.0.0.0 --port 8000" `
    -Cwd $RepoRoot `
    -StdoutLog (Join-Path $logsDir "api.stdout.log") `
    -StderrLog (Join-Path $logsDir "api.stderr.log")
& nssm set ForexEAApi AppEnvironmentExtra "PYTHONUTF8=1" | Out-Null

# --- Start -----------------------------------------------------------------
Write-Host "Starting ForexEAApi"
Start-Service ForexEAApi

Start-Sleep -Seconds 3
Get-Service ForexEAApi | Format-Table -AutoSize

Write-Host ""
Write-Host "API installed. The bot is NOT a service -- register its task with:"
Write-Host "  .\deploy\bot-task-install.ps1"
Write-Host "  .\deploy\watchdog-install.ps1"
Write-Host "Then verify with:"
Write-Host "  python deploy\healthcheck.py"
