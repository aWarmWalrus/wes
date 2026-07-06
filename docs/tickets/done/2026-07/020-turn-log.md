---
id: 020
title: Turn log — recent queries/replies/tools + /turns + dashboard table
status: done
priority: med
created: 2026-07-05
closed: 2026-07-05
tags: [observability, turns]
related: [docs/observability.md, 98c9991]
---

## What shipped
One JSONL record per completed exchange (`turns.jsonl`): ts, channel,
transcript, reply, tools run, escalated y/n — logged from the `record_turn()`
choke point (voice/discord/text), with tool/escalation capture via a
thread-local notepad. `GET /turns?n=&channel=` serves the tail; a Grafana
"Recent turns" table renders it via the Infinity datasource plugin.

## Outcome
Stores household content, so it's a **size-capped rolling window** (self-trims to
`WES_TURNS_MAX`=2000 past ~4MB), unlike the append-forever usage ledger. This is
the tool that diagnosed the three issues now tracked as #001-#003.
