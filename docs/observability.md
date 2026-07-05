# Observability — Prometheus + Grafana

Live and historical utilization graphs for both hosts, viewable from any LAN
browser. Built 2026-07-05 (phases 1–2; phase 3 — WES app metrics — is planned).

**Dashboard: <http://10.0.0.79:3000> → WES → "WES Overview"** (login `admin` /
`admin` until the owner changes it at first login; LAN-only, no port forwarding).

## Architecture

Everything long-running lives on the Pi (16GB RAM, ~2GB used); the PC only runs
two lightweight exporters so gaming headroom is unaffected.

| What | Where | Port | Runs as |
|------|-------|------|---------|
| Prometheus (scrapes + stores, 15s interval, 15d retention) | Pi | 9090 | apt `prometheus`, systemd |
| Grafana (graphs) | Pi | 3000 | apt `grafana` (grafana.com repo), systemd |
| node_exporter (Pi CPU/mem/temp/disk/net) | Pi | 9100 | apt `prometheus-node-exporter`, systemd |
| windows_exporter v0.31.7 (PC CPU/RAM/disk/net) | PC | 9182 | "WES Exporters" scheduled task |
| nvidia_gpu_exporter v1.9.1 (GPU util/VRAM/temp/power via nvidia-smi) | PC | 9835 | same task |
| `wes_server` `/metrics` (token/call counters by model/source/channel) | PC | 8080 | "WES Server" task |

- PC binaries: `C:\Users\awarm\wes-pc\bin\`; launcher
  `C:\Users\awarm\wes-pc\run_exporters.ps1` (PC-local, not in the repo — same
  pattern as `run_server.ps1`). windows_exporter runs a trimmed collector set
  (`cpu,memory,os,logical_disk,net,thermalzone`); note the old `cs` collector
  was removed in v0.31 (its metrics moved into `os`).
- Windows Firewall: inbound rule **"WES exporters (Prometheus scrape from Pi)"**
  allows TCP 9182+9835 from 10.0.0.79 only. Gotcha: when the exporters first
  listened, Windows auto-created per-exe **Block** rules (the dismissed
  connection popup) which override any Allow — those were deleted. If scrapes
  ever go down after an exporter update/rename, check for reborn block rules:
  `Get-NetFirewallApplicationFilter | ? Program -match exporter | Get-NetFirewallRule`.
- Prometheus scrape jobs (`/etc/prometheus/prometheus.yml` on the Pi):
  `node` (localhost:9100), `pc_windows` (10.0.0.168:9182), `pc_gpu`
  (10.0.0.168:9835). Validate + apply:
  `promtool check config /etc/prometheus/prometheus.yml && sudo systemctl reload prometheus`.
  Target health: <http://10.0.0.79:9090/targets>.

## Dashboard provisioning

The dashboard JSON is versioned in the repo at
`observability/dashboards/wes-overview.json` and file-provisioned
(`/etc/grafana/provisioning/dashboards/wes.yaml` → `/var/lib/grafana/dashboards/`),
so it's **read-only in the UI**. To change it:

```bash
# edit observability/dashboards/wes-overview.json (Z:\wes\... on the PC), then:
ssh walrus-pi "sudo cp ~/claude/wes/observability/dashboards/wes-overview.json \
  /var/lib/grafana/dashboards/ && sudo systemctl restart grafana-server"
```

(Ad-hoc dashboards created in the UI are fine too — they just aren't versioned.)

Live vs cumulative: gauges (CPU%, VRAM, temp) graph directly; for counters use
`rate(x[1m])` for live rates and `increase(x[$__range])` for cumulative totals
over the visible time range.

## Restart / debug

```bash
ssh walrus-pi "systemctl status prometheus prometheus-node-exporter grafana-server"
ssh walrus-pi "sudo systemctl restart grafana-server"   # or prometheus
```
```powershell
Stop-ScheduledTask -TaskName "WES Exporters"; Start-ScheduledTask -TaskName "WES Exporters"
Get-Content C:\Users\awarm\wes-pc\logs\exporters.log -Tail 10          # nvidia exporter
Get-Content C:\Users\awarm\wes-pc\logs\windows_exporter.err.log -Tail 10
```

## WES app metrics (phase 3a — built 2026-07-05)

`GET /metrics` on the server (`prometheus_client`, pinned in the wes-pc venv)
exposes `wes_llm_tokens_total{direction,model,source,channel}` and
`wes_llm_calls_total{model,source,channel}`, incremented in `record_usage()` —
the exact same events as the CSV ledger. Counters reset when the server
restarts; the dashboard uses `rate()`/`increase()` which absorb resets, and
`GET /usage` (backed by the CSV) remains the all-time source of truth. The
"WES tokens" dashboard row prices local tokens at Haiku rates ($1/$5 per MTok,
cached 2026-06-24) — if `wes_server.py`'s pricing constants change, update the
"Est. $ saved" panel expression too.

## Still planned (phase 3b)

- Turn-latency histograms by channel (stt/ttfa/total) on `/metrics`.
- Alerting (GPU temp, disk, service down) delivered through the Discord bot.
