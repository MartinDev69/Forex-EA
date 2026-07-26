# Register the trading loop as an INTERACTIVE scheduled task.
#
# The bot cannot be a Windows service. MT5 only initialises reliably when a real
# desktop session exists; from session 0 -- where services run -- mt5.initialize()
# returns (-10005, 'IPC timeout') indefinitely. So main.py runs in the interactive
# session of an auto-logged-on user, launched by deploy\run-bot.cmd.
#
# Idempotent -- re-running replaces the existing task.
#
# Requires autologon so a desktop session exists unattended after a reboot; see
# deploy/README.md "Durable runtime". Without it the box boots to a lock screen
# and this task never fires, while the API service comes up as normal.

param(
    [string]$TaskName = "ForexEA-Bot",
    [string]$User = "$env:USERNAME"
)

$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

$wrapper = Join-Path $PSScriptRoot "run-bot.cmd"
if (-not (Test-Path $wrapper)) { throw "Missing launcher at $wrapper" }

$venvPython = Join-Path $RepoRoot "venv\Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    throw "venv missing at $venvPython -- run .\deploy\install.ps1 first."
}

$logsDir = Join-Path $RepoRoot "logs"
if (-not (Test-Path $logsDir)) { New-Item -ItemType Directory -Path $logsDir | Out-Null }

# Refuse to coexist with the retired service -- two live main.py instances would
# place duplicate orders on the same account.
$legacyBot = Get-Service -Name "ForexEABot" -ErrorAction SilentlyContinue
if ($legacyBot -and $legacyBot.Status -eq "Running") {
    throw "ForexEABot service is running. Stop and remove it before registering this task."
}

$action = New-ScheduledTaskAction -Execute $wrapper

# AtLogOn, because autologon supplies the session this task needs.
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $User

# LogonType Interactive is the load-bearing flag. "Run whether user is logged on
# or not" (S4U/Password) lands the task in session 0 and MT5 IPC-timeouts forever.
$principal = New-ScheduledTaskPrincipal -UserId $User -LogonType Interactive -RunLevel Highest

# ExecutionTimeLimit 0 = never kill it; RestartCount/Interval relaunch main.py if
# it exits. The watchdog is the outer safety net for a process that is up but wedged.
$settings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Seconds 0) `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable

Register-ScheduledTask -TaskName $TaskName `
    -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null

# New-ScheduledTaskSettingsSet silently drops RestartCount/RestartInterval --
# Register-ScheduledTask writes the task with both fields empty, so the bot gets
# no auto-restart at all and a crash leaves it down until the watchdog notices.
# Verified on 2026-07-26: the registered XML had <RestartCount/> empty despite
# being passed 999. Patch them into the XML and re-register.
# The restart policy lives in the XML as a <RestartOnFailure> container with
# <Interval> and <Count> children -- NOT the flat RestartCount/RestartInterval
# that New-ScheduledTaskSettingsSet accepts. Writing the flat names back is
# rejected with "The task XML contains an unexpected node".
$xml = [xml](Export-ScheduledTask -TaskName $TaskName)
$ns = $xml.DocumentElement.NamespaceURI
$s = $xml.Task.Settings
if (-not $s.SelectSingleNode("*[local-name()='RestartOnFailure']")) {
    $rof = $xml.CreateElement("RestartOnFailure", $ns)
    $iv = $xml.CreateElement("Interval", $ns); $iv.InnerText = "PT1M"
    $ct = $xml.CreateElement("Count", $ns);    $ct.InnerText = "999"
    # Schema order: Interval before Count, and RestartOnFailure last in Settings.
    $rof.AppendChild($iv) | Out-Null
    $rof.AppendChild($ct) | Out-Null
    $s.AppendChild($rof) | Out-Null
    Register-ScheduledTask -TaskName $TaskName -Xml $xml.OuterXml -User $User -Force | Out-Null
    $chk = ([xml](Export-ScheduledTask -TaskName $TaskName)).Task.Settings.RestartOnFailure
    Write-Host "Restart policy: Count=$($chk.Count) Interval=$($chk.Interval)"
}

Write-Host "Registered scheduled task $TaskName (user=$User, Interactive, AtLogOn)"
Get-ScheduledTask -TaskName $TaskName |
    Select-Object TaskName, State,
        @{n = "RunAs";   e = { $_.Principal.UserId } },
        @{n = "Session"; e = { $_.Principal.LogonType } } |
    Format-Table -AutoSize

Write-Host "Start it now with:  Start-ScheduledTask -TaskName $TaskName"
Write-Host "Confirm autologon:  Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon' | Select AutoAdminLogon, DefaultUserName"
