---
id: 019
title: App token metrics — /metrics + WES tokens dashboard row
status: done
priority: med
created: 2026-07-05
closed: 2026-07-05
tags: [observability, metrics]
related: [docs/observability.md, bd6f75c, "#017"]
---

## What shipped
`GET /metrics` on wes_server (`prometheus_client`) exposes
`wes_llm_tokens_total{direction,model,source,channel}` and `wes_llm_calls_total`,
incremented in `record_usage()` — same events as the CSV ledger (#017).
Prometheus scrapes it; a "WES tokens" dashboard row shows token rate by
model/source, tokens-in-range, and est. $ saved vs Haiku.

## Outcome
Counters reset on restart; Grafana rate()/increase() absorb that. `GET /usage`
(CSV) remains the all-time source of truth. Latency histograms are the open
follow-up (#006).
