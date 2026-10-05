# Restart the Zingy GPU worker (demucs_relay) detached — survives the SSH session.
$h   = $env:USERPROFILE
$dir = Join-Path $h "demucs_relay"
$py  = Join-Path $dir "venv\Scripts\python.exe"
$wk  = Join-Path $dir "worker.py"
Get-Process python -ErrorAction SilentlyContinue | Stop-Process -Force
Start-Sleep -Seconds 2
Start-Process -FilePath $py -ArgumentList "-u", $wk -WorkingDirectory $dir -WindowStyle Hidden `
  -RedirectStandardOutput (Join-Path $dir "worker.log") `
  -RedirectStandardError  (Join-Path $dir "worker.err")
Start-Sleep -Seconds 5
Write-Output "=== worker.log ==="
Get-Content (Join-Path $dir "worker.log") -Tail 8 -ErrorAction SilentlyContinue
Write-Output "=== worker.err (tail) ==="
Get-Content (Join-Path $dir "worker.err") -Tail 4 -ErrorAction SilentlyContinue
Write-Output "=== python running ==="
(Get-Process python -ErrorAction SilentlyContinue | Measure-Object).Count
