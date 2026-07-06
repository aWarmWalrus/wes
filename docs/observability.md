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

Gotcha (bitten 2026-07-05): scheduled-task PowerShell actions **must include
`-WindowStyle Hidden`** — without it the task runs in a visible console, and
closing that window kills the service with exit 0xC000013A, which does NOT
trigger the task's auto-restart (it's a normal termination, not a failure).
All four WES tasks now run hidden; keep it that way for new ones.

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

## Turn log — recent queries/replies/tools (built 2026-07-05)

The dashboard's "Recent turns" table shows the last exchanges verbatim: query,
reply, which tools ran, escalated y/n, per channel. Pipeline:

- `wes_server` logs one JSONL record per completed exchange to
  `C:\Users\awarm\wes-pc\logs\turns.jsonl` (env `WES_TURNS_LOG`), from the same
  `record_turn()` choke point as conversation memory — voice, Discord, and
  text all flow through it. Tool calls and escalations are captured via a
  thread-local notepad (`_turn_begin`/`_note_tool`/`_note_escalation`).
- **This file stores content** — transcripts of everything said in the house —
  so unlike `usage.csv` it is a rolling window, not append-forever: past ~4MB
  it trims itself to the last `WES_TURNS_MAX` (default 2000) exchanges.
- `GET /turns?n=20&channel=voice` serves the tail, newest first (also handy
  from curl or a future Discord `!turns`).
- Grafana renders it via the **Infinity datasource** plugin
  (`yesoreyeram-infinity-datasource`, installed with
  `sudo grafana cli --homepath /usr/share/grafana plugins install ...` +
  restart; provisioned in `/etc/grafana/provisioning/datasources/infinity.yaml`).
  The table panel polls `http://10.0.0.168:8080/turns?n=15` — it shows the
  *current* tail regardless of the dashboard's time range.

## Alerting — Jarvis DMs the owner (built 2026-07-05)

Division of labor: **Prometheus evaluates, the Discord bot delivers.**

- Rules live in the repo at `observability/prometheus/wes-alerts.yml`
  (deploy command in its header) → `/etc/prometheus/rules/` on the Pi.
  Current set: `TargetDown` (any scrape target, 5m), `GPUHot` (>85°C 5m),
  `PiHot` (>80°C 5m), `PiDiskLow` / `PCDiskLow` (<10% free 30m). Prometheus
  owns thresholds, `for:` durations, and flap suppression — state at
  <http://10.0.0.79:9090/alerts>.
- `wes_discord.py` runs an `alert_watch` task: polls Prometheus'
  `GET /api/v1/alerts` every 60s (`WES_PROM_URL`, `WES_ALERT_POLL_S`) and
  notifies the owner on changes. One DM per (rule, instance); a still-firing
  alert never repeats. If Prometheus itself is unreachable 5 polls in a row it
  DMs that too (the watchdog needs a watchdog), and again on recovery.
- **Alerts are phrased by Jarvis, not sent raw.** On a change the bot builds a
  grounded event string — the rule's own `summary`, the affected host, and a
  plain-English description of what the rule means (`ALERT_CONTEXT` in
  `wes_discord.py`, keep it in sync with the rules) — and POSTs it to the
  server's **`POST /announce`**. The server has Jarvis explain it in his own
  voice AND **records it into the "discord" conversation memory**, so a reply
  like "what was that?" has context (without `/announce` the DM would bypass
  the server and Jarvis would have no memory of sending it). If the server is
  unreachable (which may be *why* an alert fired) the bot falls back to a raw
  `🚨 WES alert: <summary>` DM — an alert is never lost.
- `/announce` (`{"event", "channel"}` → `{"reply"}`) is the general proactive-
  notification primitive; scheduled/deferred actions will reuse it.
- No Alertmanager: for one owner and five rules, the bot-as-receiver keeps it
  to zero new services. Revisit if rules need routing/grouping/silences.
- Verified live 2026-07-05: stopped the PC exporters → two TargetDown DMs
  after 5m → restarted → two Resolved DMs.
- **Encoding gotcha:** under the scheduled task, Python's stdout is cp1252, so
  `print()`-ing an emoji or any non-Latin-1 char raises `UnicodeEncodeError`
  mid-handler. The first real alert died on its own 🚨 this way. Both
  `wes_discord.py` and `wes_server.py` now `reconfigure(errors="replace")`
  their streams in `main`, and the alert watcher logs with `!a` (ASCII repr).
  Keep any new console print in the services ASCII-safe.

## Still planned (phase 3b)

- Turn-latency histograms by channel (stt/ttfa/total) on `/metrics`.
