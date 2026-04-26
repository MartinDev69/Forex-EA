# One-time setup for the Forex-EA bot on a Windows VPS.
#
# What it does:
#   1. Verifies Python 3.12 is on PATH (MetaTrader5 wheels aren't on 3.13 yet).
#   2. Creates the project venv if missing.
#   3. Installs requirements.txt.
#   4. Creates data/, logs/, data/models/ if missing.
#   5. Copies .env.example -> .env when .env doesn't exist (you still have to fill it in).
#
# Run from an elevated PowerShell in the repo root:
#   .\deploy\install.ps1

$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot
Write-Host "Repo root: $RepoRoot"

# --- Python version check -------------------------------------------------
$pythonCmd = Get-Command py -ErrorAction SilentlyContinue
if (-not $pythonCmd) { $pythonCmd = Get-Command python -ErrorAction SilentlyContinue }
if (-not $pythonCmd) {
    throw "Python not found on PATH. Install Python 3.12 from python.org and re-run."
}

$versionOut = & $pythonCmd.Source -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
if ($versionOut -ne "3.12") {
    Write-Warning "Python $versionOut found; 3.12 is required for MetaTrader5 wheels."
    Write-Warning "Install Python 3.12 and make sure it's the default on PATH (or use py -3.12)."
    throw "Wrong Python version."
}
Write-Host "Python 3.12 OK"

# --- venv ------------------------------------------------------------------
$venv = Join-Path $RepoRoot "venv"
if (-not (Test-Path $venv)) {
    Write-Host "Creating venv at $venv"
    & $pythonCmd.Source -m venv $venv
}
$venvPython = Join-Path $venv "Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    throw "venv python missing at $venvPython — venv creation failed."
}

# --- dependencies ----------------------------------------------------------
Write-Host "Upgrading pip/wheel"
& $venvPython -m pip install --upgrade pip wheel | Out-Host
Write-Host "Installing requirements.txt"
& $venvPython -m pip install -r (Join-Path $RepoRoot "requirements.txt") | Out-Host

# --- directories -----------------------------------------------------------
foreach ($dir in @("data", "data\models", "data\bars", "logs")) {
    $p = Join-Path $RepoRoot $dir
    if (-not (Test-Path $p)) { New-Item -ItemType Directory -Path $p | Out-Null }
}

# --- .env ------------------------------------------------------------------
$envPath = Join-Path $RepoRoot ".env"
$envExample = Join-Path $RepoRoot ".env.example"
if (-not (Test-Path $envPath)) {
    if (Test-Path $envExample) {
        Copy-Item $envExample $envPath
        Write-Host "Created .env from .env.example — edit it now before starting the bot."
    } else {
        Write-Warning ".env.example not found. Create a .env manually."
    }
} else {
    Write-Host ".env already exists — leaving it alone."
}

# --- AUTH_SECRET auto-fill ------------------------------------------------
# The API refuses to start without one. Generate if the slot is empty so the
# operator can't accidentally boot a service with no signing key.
$envContent = Get-Content $envPath -Raw
if ($envContent -match "(?m)^AUTH_SECRET=\s*$") {
    Write-Host "AUTH_SECRET is empty — generating one"
    $newSecret = & $venvPython -c "import secrets; print(secrets.token_urlsafe(48))"
    $envContent = [regex]::Replace($envContent, "(?m)^AUTH_SECRET=\s*$", "AUTH_SECRET=$newSecret")
    Set-Content -Path $envPath -Value $envContent -NoNewline
    Write-Host "AUTH_SECRET written to .env"
} elseif ($envContent -notmatch "(?m)^AUTH_SECRET=") {
    Write-Warning "AUTH_SECRET line missing from .env entirely. Add one manually."
}

# --- quick sanity: can we import the bot? ----------------------------------
Write-Host "Importing the bot package to verify the install"
& $venvPython -c "import src.bot, src.execution.mt5_live, src.ml.signal_filter; print('imports OK')" | Out-Host

Write-Host ""
Write-Host "Install complete. Next steps:"
Write-Host "  1. Edit .env with your real MT5 credentials."
Write-Host "  2. Register services:  .\deploy\service-install.ps1"
Write-Host "  3. Run healthcheck:    python deploy\healthcheck.py"
