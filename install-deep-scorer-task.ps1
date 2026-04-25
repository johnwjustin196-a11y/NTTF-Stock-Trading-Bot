# Registers the Deep Scorer as a Windows Scheduled Task.
# Runs every Sunday at 9:00 PM and catches up automatically if the
# computer was off or asleep at that time (StartWhenAvailable).
#
# Right-click this file -> "Run with PowerShell".
# If Windows complains about execution policy, run first in PowerShell:
#    Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
#
# To remove the task later:
#    Unregister-ScheduledTask -TaskName "StockDeepScorer" -Confirm:$false

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = (Get-Command python -ErrorAction SilentlyContinue).Source

if (-not $python) {
    Write-Host "ERROR: Python not found on PATH. Install Python first, then run setup.bat." -ForegroundColor Red
    Read-Host "Press Enter to exit"
    exit 1
}

$taskName = "StockDeepScorer"

$action = New-ScheduledTaskAction `
    -Execute $python `
    -Argument "-m src.analysis.deep_scorer" `
    -WorkingDirectory $projectRoot

# Weekly trigger: every Sunday at 9 PM
# StartWhenAvailable means: if the machine was off at 9 PM Sunday,
# run as soon as it next comes online (login, wake from sleep, etc.)
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Sunday -At "9:00PM"

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2) `
    -RestartCount 2 `
    -RestartInterval (New-TimeSpan -Minutes 10)

$principal = New-ScheduledTaskPrincipal `
    -UserId $env:USERNAME `
    -LogonType Interactive `
    -RunLevel Limited

# Remove existing registration if present
if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
    Write-Host "Removing previous '$taskName' task registration..."
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
}

Register-ScheduledTask `
    -TaskName $taskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Description "Scores all watchlist stocks across 5 dimensions (technical, fundamental, sentiment, risk, thesis) using yfinance + local LLM. Results saved to data/trade_scores.json for use by the trading bot."

Write-Host ""
Write-Host "============================================================" -ForegroundColor Green
Write-Host "  Scheduled task '$taskName' registered." -ForegroundColor Green
Write-Host ""
Write-Host "  Runs: Every Sunday at 9:00 PM" -ForegroundColor Green
Write-Host "  Catch-up: If the PC was off Sunday night, it runs" -ForegroundColor Green
Write-Host "  automatically the next time you log in." -ForegroundColor Green
Write-Host ""
Write-Host "  Run it right now with:" -ForegroundColor Green
Write-Host "    Start-ScheduledTask -TaskName $taskName" -ForegroundColor Cyan
Write-Host ""
Write-Host "  Or use the batch file:" -ForegroundColor Green
Write-Host "    run-deep-scorer.bat" -ForegroundColor Cyan
Write-Host ""
Write-Host "  Scores saved to: data\trade_scores.json" -ForegroundColor Green
Write-Host "  Valid for 7 days (configurable in settings.yaml)" -ForegroundColor Green
Write-Host "============================================================" -ForegroundColor Green
Write-Host ""
Read-Host "Press Enter to exit"
