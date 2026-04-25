# Registers the bot as a Windows Scheduled Task so it auto-starts at boot
# and runs in the background without keeping a console window open.
#
# Right-click this file -> "Run with PowerShell".
# If Windows complains about execution policy, run this first in PowerShell:
#    Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
#
# To remove the task later:
#    Unregister-ScheduledTask -TaskName "StockTradingBot" -Confirm:$false

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = (Get-Command python -ErrorAction SilentlyContinue).Source

if (-not $python) {
    Write-Host "ERROR: Python not found on PATH. Install Python first, then run setup.bat." -ForegroundColor Red
    Read-Host "Press Enter to exit"
    exit 1
}

$taskName = "StockTradingBot"
$action = New-ScheduledTaskAction `
    -Execute $python `
    -Argument "-m src.main --mode sim" `
    -WorkingDirectory $projectRoot

# Trigger: at user logon, and also at 4:00 AM daily as a safety net
$triggers = @(
    New-ScheduledTaskTrigger -AtLogOn
    New-ScheduledTaskTrigger -Daily -At 4:00AM
)

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 5)

$principal = New-ScheduledTaskPrincipal `
    -UserId $env:USERNAME `
    -LogonType Interactive `
    -RunLevel Limited

# Remove existing registration if present
if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
    Write-Host "Removing previous task registration..."
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
}

Register-ScheduledTask `
    -TaskName $taskName `
    -Action $action `
    -Trigger $triggers `
    -Settings $settings `
    -Principal $principal `
    -Description "Stock trading bot - runs scheduler in sim mode. Edit settings.yaml to change mode."

Write-Host ""
Write-Host "============================================================" -ForegroundColor Green
Write-Host "  Scheduled task 'StockTradingBot' registered." -ForegroundColor Green
Write-Host ""
Write-Host "  It will auto-start at your next login and stay running" -ForegroundColor Green
Write-Host "  in the background. You can start it right now with:" -ForegroundColor Green
Write-Host ""
Write-Host "    Start-ScheduledTask -TaskName StockTradingBot" -ForegroundColor Cyan
Write-Host ""
Write-Host "  View it in Task Scheduler (search Start menu) under" -ForegroundColor Green
Write-Host "  Task Scheduler Library." -ForegroundColor Green
Write-Host "============================================================" -ForegroundColor Green
Write-Host ""
Read-Host "Press Enter to exit"
