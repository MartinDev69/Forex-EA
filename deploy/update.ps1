# In-place redeploy: pull latest code, refresh deps, restart the bot task + API service.
#
# Run from the repo root on the VPS:
#   .\deploy\update.ps1
#
# Safe to run while the bot is live -- things get restarted in a controlled order.

$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

# --- Pull latest -----------------------------------------------------------
Write-Host "Fetching and fast-forwarding main"
git fetch --quiet
git status --porcelain
if ($LASTEXITCODE -ne 0) { throw "git fetch failed" }

$dirty = git status --porcelain
if ($dirty) {
    Write-Warning "Working tree has uncommitted changes:"
    Write-Warning $dirty
    throw "Refusing to update with a dirty tree -- stash/commit first."
}

git pull --ff-only
if ($LASTEXITCODE -ne 0) { throw "git pull failed" }

# --- Refresh deps ----------------------------------------------------------
$venvPython = Join-Path $RepoRoot "venv\Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    throw "venv missing at $venvPython -- run deploy\install.ps1 first."
}
Write-Host "Reinstalling requirements (upgrade-only-if-needed)"
& $venvPython -m pip install -r (Join-Path $RepoRoot "requirements.txt") | Out-Host

# --- Restart the runtime ---------------------------------------------------
# The bot is an interactive scheduled task, not a service (MT5 needs a desktop
# session); only the API is an NSSM service. See CLAUDE.md "Where it runs".
# Bot first (stops placing new orders), then API.
& (Join-Path $PSScriptRoot "service-control.ps1") restart

Write-Host ""
Write-Host "Update complete. Verify with:"
Write-Host "  python deploy\healthcheck.py"
