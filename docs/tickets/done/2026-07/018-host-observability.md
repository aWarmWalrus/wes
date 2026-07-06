---
id: 018
title: Host observability — Prometheus + Grafana + exporters
status: done
priority: med
created: 2026-07-05
closed: 2026-07-05
tags: [observability, prometheus, grafana]
related: [docs/observability.md, b8829f7]
---

## What shipped
Prometheus + Grafana on the Pi (apt/systemd), scraping node_exporter (Pi) and
windows_exporter :9182 + nvidia_gpu_exporter :9835 (PC, under the "WES Exporters"
scheduled task). Dashboard "WES Overview" at http://10.0.0.79:3000 — PC
CPU/RAM/GPU/VRAM/temp/power, Pi CPU/RAM/temp. Dashboard JSON versioned at
`observability/dashboards/wes-overview.json`, file-provisioned.

## Gotchas learned (documented in docs/observability.md)
- windows_exporter v0.31 dropped the `cs` collector (moved into `os`).
- Windows auto-created per-exe **Block** firewall rules that override the Allow.
- Scheduled tasks need `-WindowStyle Hidden` — a visible console, when closed,
  kills the service with no auto-restart (0xC000013A).
