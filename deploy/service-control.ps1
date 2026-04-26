# Start/stop/status helpers for the Forex-EA services.
#
#   .\deploy\service-control.ps1 start
#   .\deploy\service-control.ps1 stop
#   .\deploy\service-control.ps1 status
#   .\deploy\service-control.ps1 logs bot   # tail bot stderr
#   .\deploy\service-control.ps1 logs api   # tail api stderr

param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("start", "stop", "restart", "status", "logs")]
    [string]$Action,

    [Parameter(Position = 1)]
    [ValidateSet("bot", "api")]
    [string]$Target
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
$logsDir = Join-Path $RepoRoot "logs"
$Services = @("ForexEABot", "ForexEAApi")

switch ($Action) {
    "start"   { $Services | ForEach-Object { Start-Service $_ } }
    "stop"    {
        # Reverse order: stop bot first so it quits cleanly while API is still up.
        [array]::Reverse($Services)
        $Services | ForEach-Object { Stop-Service $_ -Force }
    }
    "restart" { $Services | ForEach-Object { Restart-Service $_ } }
    "status"  { Get-Service $Services | Format-Table -AutoSize }
    "logs"    {
        if (-not $Target) { throw "logs requires a target: bot or api" }
        $file = Join-Path $logsDir "$Target.stderr.log"
        if (-not (Test-Path $file)) { throw "log file not found: $file" }
        Get-Content -Path $file -Wait -Tail 50
    }
}
