---
id: 022
title: Single source of truth for host IPs/ports — hosts.yaml + lookup_hosts
status: done
priority: med
created: 2026-07-06
closed: 2026-07-06
tags: [config, infra, tools]
related: [3366320, hosts.yaml, wes_hosts.py]
---

## What shipped
`hosts.yaml` (repo root) is the single source of truth for machine identities,
IPs, and service ports; `wes_hosts.py` loads it. The PC server
(PI_STATE_URL/OLLAMA_URL), Discord bot (SERVER_URL/PROM_URL), and Pi client
(PC_URL/HEALTH_URL/STREAM_URL/etc) all derive addresses from it — env vars still
override, and every lookup has a `default` so a missing/unreadable registry
degrades gracefully instead of bricking a service. PyYAML added to the PC venv +
the Pi client venv.

Jarvis reads it via a new no-arg **`lookup_hosts` tool** (returns
`wes_hosts.summary()`) — the router decides when to call it, so the mapping is
NOT hard-coded into his prompt (owner's explicit preference).

## Outcome
Verified live: "what's the Pi's IP and grafana's port?" → `[tool] lookup_hosts`
→ "10.0.0.79 ... port 3000". Two configs still restate addresses (can't read the
yaml): the Pi's Prometheus scrape targets and the Grafana dashboard `/turns` URL
— keep in sync if IPs change.
