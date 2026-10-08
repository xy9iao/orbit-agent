Stop-ScheduledTask -TaskName "Orbit" -ErrorAction SilentlyContinue
Unregister-ScheduledTask -TaskName "Orbit" -Confirm:$false
Write-Host "Orbit task removed."
