# WES metrics exporters — started at logon by the "WES Exporters" scheduled task.
# Runs windows_exporter (:9182, host CPU/RAM/disk/net) and nvidia_gpu_exporter
# (:9835, GPU util/VRAM/temp via nvidia-smi). Scraped by Prometheus on the Pi
# (10.0.0.79:9090); graphs in Grafana at http://10.0.0.79:3000.
# See docs/observability.md in the repo.

$base = "C:\Users\awarm\wes-pc"
$logdir = "$base\logs"
New-Item -ItemType Directory -Force -Path $logdir | Out-Null

$stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
Set-Content "$logdir\exporters.log" "==== WES exporters starting $stamp ===="

# Lightweight collector set only — keep PC-side overhead minimal.
$winArgs = "--collectors.enabled cpu,memory,os,logical_disk,net,thermalzone"
Start-Process -NoNewWindow -FilePath "$base\bin\windows_exporter.exe" `
    -ArgumentList $winArgs `
    -RedirectStandardOutput "$logdir\windows_exporter.log" `
    -RedirectStandardError "$logdir\windows_exporter.err.log"

# nvidia_gpu_exporter polls nvidia-smi; runs in the foreground so the task
# stays alive and restarts both on failure.
& "$base\bin\nvidia_gpu_exporter.exe" *>> "$logdir\exporters.log"
