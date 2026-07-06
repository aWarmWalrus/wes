---
id: 017
title: Token usage ledger + /usage
status: done
priority: med
created: 2026-07-05
closed: 2026-07-05
tags: [observability, cost]
related: [docs/pipeline.md, "#019"]
---

## What shipped
Every LLM call appends one row to `usage.csv` (ts, model, source =
router/escalate/vlm/claude, channel, tokens in/out — Ollama reports counts on
its final chunk, Claude in `response.usage`). `GET /usage` (`?days=N`) rolls it
up by model+source and prices local tokens at claude-haiku-4-5 rates ($1/$5 per
MTok, cached 2026-06-24) to estimate the "saving" vs the Claude API; claude rows
are actual spend.

## Outcome
Working live; it's what surfaced the announced-escalation bug that became #002's
predecessor fix. Later exposed as Prometheus counters (#019). Caveat: gemma and
Claude tokenize differently — the saving is a rough estimate by design.
