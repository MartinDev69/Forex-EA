# Register the bot + API as Windows services using NSSM.
#
# Prerequisites:
#   * NSSM installed and on PATH (https://nssm.cc -- chocolatey: `choco install nssm`).
#   * .\deploy\install.ps1 has been run (venv exists).
#
# Services created:
#   ForexEABot -- runs main.py in a loop.
#   ForexEAApi -- uvicorn serving src.api.server:app on port 8000.
#
# Both run as the current user by default. For production, use
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

# --- Bot --------------------------------------------------------------------
Install-Svc `
    -Name "ForexEABot" `
    -Exe $venvPython `
    -AppArgs "main.py" `
    -Cwd $RepoRoot `
    -StdoutLog (Join-Path $logsDir "bot.stdout.log") `
    -StderrLog (Join-Path $logsDir "bot.stderr.log")

# Environment for the bot service -- tells main.py to use real MT5 and load the ML model if present.
# PYTHONUTF8=1 forces Python's UTF-8 mode so log lines with non-ASCII chars don't
# blow up when Windows' default code page is cp1252 (services have no console).
& nssm set ForexEABot AppEnvironmentExtra "USE_MT5=1" "PYTHONUTF8=1" | Out-Null

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
Write-Host "Starting services"
Start-Service ForexEAApi
Start-Service ForexEABot

Start-Sleep -Seconds 3
Get-Service ForexEABot, ForexEAApi | Format-Table -AutoSize

Write-Host ""
Write-Host "Done. Verify with:"
Write-Host "  python deploy\healthcheck.py"
