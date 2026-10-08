# Registers Orbit as a Windows scheduled task: starts at logon, restarts on failure.
# Run from an elevated PowerShell:  .\scripts\install_task.ps1
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$python = Join-Path $root ".venv\Scripts\pythonw.exe"
if (-not (Test-Path $python)) { throw "venv not found at $python - run: python -m venv .venv; .venv\Scripts\pip install -e ." }

$action   = New-ScheduledTaskAction -Execute $python -Argument "-m orbit" -WorkingDirectory $root
$trigger  = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
            -ExecutionTimeLimit ([TimeSpan]::Zero) -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName "Orbit" -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null
Start-ScheduledTask -TaskName "Orbit"
Write-Host "Orbit task registered and started. Logs: $root\logs\orbit.log"
Write-Host "Tip: set Windows power plan to never sleep so Orbit stays online."
