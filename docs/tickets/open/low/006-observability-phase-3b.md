---
id: 006
title: Observability phase 3b — latency histograms + more alert rules
status: open
priority: low
created: 2026-07-05
closed:
tags: [observability, metrics, alerting]
related: [docs/observability.md, "#021"]
---

## Goal
Extend the built observability stack (Prometheus + Grafana; token metrics and
turn log are done — see done tickets #018-#021).

## Approach
- Add turn-latency histograms by channel (stt / ttfa / total) to `/metrics` in
  `wes_server.py` via `prometheus_client`, then a latency row on the dashboard.
- More alert rules in `observability/prometheus/wes-alerts.yml` as needs appear
  (e.g. eval-score regression, server-restart-loop, disk-growth rate). Add the
  matching `ALERT_CONTEXT` entry in `wes_discord.py` for each so Jarvis can
  explain it.
- Consider a disk-free panel/alert (the retention audit flagged Pi journald at
  3.1GB and no rotation on some logs).

## Acceptance
- [ ] latency histograms scraped and graphed
- [ ] any new rule has an ALERT_CONTEXT entry

## Notes
Grafana/Prometheus configs still restate host addresses (can't read
`hosts.yaml`) — keep them in sync if IPs change.
