# One-shot bootstrap for a fresh Windows VPS — UAT only.
#
# Walks through:
#   1. Prereq sanity check (Python 3.12, Git, NSSM, MT5 terminal).
#   2. install.ps1  (venv, deps, .env, AUTH_SECRET).
#   3. Validates that .env has real MT5 creds (refuses to continue with placeholders).
#   4. service-install.ps1  (registers ForexEABot + ForexEAApi).
#   5. Seeds the first admin user (interactive password prompt).
#   6. Opens TCP/8000 in Windows Firewall.
#   7. Runs healthcheck.
#
# Idempotent — safe to re-run after fixing any issue. Run from elevated PowerShell:
#   .\deploy\uat-bootstrap.ps1
#
# Skip flags for re-runs:
#   -SkipPrereqCheck  | -SkipUserSeed  | -SkipFirewall

param(
    [switch]$SkipPrereqCheck,
    [switch]$SkipUserSeed,
    [switch]$SkipFirewall
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

function Section($msg) {
    Write-Host ""
    Write-Host "=== $msg ===" -ForegroundColor Cyan
}

# --- 1. Prereqs ------------------------------------------------------------
if (-not $SkipPrereqCheck) {
    Section "Checking prerequisites"

    function Need($name, $hint) {
        if (-not (Get-Command $name -ErrorAction SilentlyContinue)) {
            throw "Missing prerequisite '$name'. $hint"
        }
        Write-Host "  $name OK"
    }

    Need "git"  "Install with: winget install --id Git.Git"
    Need "nssm" "Install with: choco install nssm  (or grab from nssm.cc)"

    $py = Get-Command py -ErrorAction SilentlyContinue
    if (-not $py) { $py = Get-Command python -ErrorAction SilentlyContinue }
    if (-not $py) { throw "Python not on PATH. Install Python 3.12 from python.org." }
    $ver = & $py.Source -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
    if ($ver -ne "3.12") {
        throw "Python $ver found; 3.12 required (MT5 wheel doesn't support 3.13)."
    }
    Write-Host "  Python 3.12 OK"

    $mt5Candidates = @(
        "C:\Program Files\Exness MetaTrader 5\terminal64.exe",
        "C:\Program Files\MetaTrader 5\terminal64.exe"
    )
    $mt5Found = $mt5Candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
    if ($mt5Found) {
        Write-Host "  MT5 terminal found: $mt5Found"
    } else {
        Write-Warning "  MT5 terminal not found. Install Exness MT5 from https://www.exness.com/platforms/mt5/ and log in once before starting the bot."
    }
}

# --- 2. install.ps1 --------------------------------------------------------
Section "Running install.ps1"
& (Join-Path $PSScriptRoot "install.ps1")

# --- 3. Validate .env ------------------------------------------------------
Section "Validating .env"
$envPath = Join-Path $RepoRoot ".env"
if (-not (Test-Path $envPath)) { throw ".env missing — install.ps1 should have created it." }

$envContent = Get-Content $envPath -Raw
$needsEdit = $false
if ($envContent -match "MT5_LOGIN=12345678")          { Write-Warning "MT5_LOGIN still has placeholder";    $needsEdit = $true }
if ($envContent -match "MT5_PASSWORD=your_demo_password") { Write-Warning "MT5_PASSWORD still has placeholder"; $needsEdit = $true }
if ($envContent -notmatch "(?m)^AUTH_SECRET=.{32,}")  { Write-Warning "AUTH_SECRET missing or too short";    $needsEdit = $true }

if ($needsEdit) {
    Write-Host ""
    Write-Warning "Open .env, fill in your Exness demo MT5 login/password/server, save, then re-run this script."
    Start-Process notepad $envPath
    exit 1
}
Write-Host "  .env looks good"

# --- 4. Services -----------------------------------------------------------
Section "Registering Windows services"
& (Join-Path $PSScriptRoot "service-install.ps1")

# --- 5. Seed admin user ----------------------------------------------------
if (-not $SkipUserSeed) {
    Section "Seeding admin user"
    Write-Host "(skip with -SkipUserSeed if you already have an admin)"
    $username = Read-Host "Admin username (blank to skip)"
    if ([string]::IsNullOrWhiteSpace($username)) {
        Write-Host "  No username entered — skipped."
    } else {
        $venvPython = Join-Path $RepoRoot "venv\Scripts\python.exe"
        & $venvPython (Join-Path $RepoRoot "scripts\create_user.py") --username $username
    }
}

# --- 6. Firewall -----------------------------------------------------------
if (-not $SkipFirewall) {
    Section "Opening firewall for TCP/8000"
    $rule = Get-NetFirewallRule -DisplayName "ForexEA API" -ErrorAction SilentlyContinue
    if ($rule) {
        Write-Host "  Rule already exists — skipping"
    } else {
        New-NetFirewallRule -DisplayName "ForexEA API" -Direction Inbound `
            -Protocol TCP -LocalPort 8000 -Action Allow | Out-Null
        Write-Host "  TCP/8000 inbound allowed"
    }
}

# --- 7. Healthcheck --------------------------------------------------------
Section "Running healthcheck"
Start-Sleep -Seconds 5  # let services warm
$venvPython = Join-Path $RepoRoot "venv\Scripts\python.exe"
& $venvPython (Join-Path $RepoRoot "deploy\healthcheck.py")

Section "UAT bootstrap complete"
Write-Host "Find this VPS's public IP from the Contabo dashboard, then point the mobile app at:"
Write-Host "  http://<vps-ip>:8000" -ForegroundColor Yellow
Write-Host ""
Write-Host "Day-to-day commands:"
Write-Host "  .\deploy\service-control.ps1 status"
Write-Host "  .\deploy\service-control.ps1 logs bot"
Write-Host "  .\deploy\update.ps1   # pull + restart"
