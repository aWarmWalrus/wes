# WES metrics exporters — started at logon by the "WES Exporters" scheduled task.
# Runs windows_exporter (:9182, host CPU/RAM/disk/net) and nvidia_gpu_exporter
# (:9835, GPU util/VRAM/temp via nvidia-smi). Scraped every 15s by the
# Prometheus container on this machine (127.0.0.1:9090); graphs in Grafana at
# http://localhost:3001. Both used to run on the Pi, which was repurposed on
# 2026-09-02 — see observability/docker-compose.yml and docs/observability.md.
#
# These still bind 0.0.0.0, not loopback: Prometheus runs inside WSL and reaches
# them across the NAT gateway, which is not a loopback path.

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
