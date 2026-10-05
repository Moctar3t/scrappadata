# Register + start the Zingy GPU worker as a Scheduled Task so it's owned by the
# Task Scheduler service — survives SSH session close AND reboots (AtLogOn trigger).
$h   = $env:USERPROFILE
$dir = Join-Path $h "demucs_relay"
$bat = Join-Path $dir "run_worker.bat"
Get-Process python -ErrorAction SilentlyContinue | Stop-Process -Force
Unregister-ScheduledTask -TaskName "ZingyWorker" -Confirm:$false -ErrorAction SilentlyContinue
$action  = New-ScheduledTaskAction -Execute "cmd.exe" -Argument ('/c "' + $bat + '"') -WorkingDirectory $dir
$trigger = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
  -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 5 -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName "ZingyWorker" -Action $action -Trigger $trigger -Settings $settings -RunLevel Highest -Force | Out-Null
Start-ScheduledTask -TaskName "ZingyWorker"
Start-Sleep -Seconds 7
Write-Output ("python running: " + (Get-Process python -ErrorAction SilentlyContinue | Measure-Object).Count)
Write-Output "=== worker.log tail ==="
Get-Content (Join-Path $dir "worker.log") -Tail 6 -ErrorAction SilentlyContinue
