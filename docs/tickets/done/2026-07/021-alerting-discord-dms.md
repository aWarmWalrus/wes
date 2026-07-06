---
id: 021
title: Alerting — Prometheus rules + Jarvis DMs the owner in natural English
status: done
priority: high
created: 2026-07-05
closed: 2026-07-06
tags: [observability, alerting, discord]
related: [docs/observability.md, 8b79410, 1c440bb, 7dcc3a6]
---

## What shipped
Prometheus evaluates alert rules (`observability/prometheus/wes-alerts.yml`:
TargetDown, GPUHot, PiHot, PiDiskLow, PCDiskLow); the Discord bot polls
`/api/v1/alerts` every 60s and notifies the owner on fire/resolve. Alerts are
**phrased by Jarvis** via a new `POST /announce` primitive: the bot builds a
grounded event (rule summary + `ALERT_CONTEXT` knowledge base) and the server
has Jarvis explain it in natural English AND **records it into conversation
memory**, so a follow-up "what was that?" has context. Falls back to a raw
summary DM if the server is down (an alert is never lost). `/announce` is the
general proactive-notification primitive (reused by #005).

## Gotchas learned
- Scheduled-task stdout is **cp1252** — printing an emoji raised
  UnicodeEncodeError and silently killed the watcher on its first real alert.
  Both services now reconfigure their streams (`errors=replace`) and log ASCII
  (`!a`). Regression test added. → the "smoke-test via the scheduled task, not
  the script" rule in the wes-reload skill.
- Voice-prompt leak: alerts spelled IPs out phonetically until `TEXT_CHANNEL_NOTE`
  was strengthened to write digits/identifiers normally (commit 7dcc3a6).

## Outcome
Verified live end-to-end: firing DMs (cause + next step) → follow-up recall from
memory → resolve DMs. More rules + latency histograms are #006.
