---
id: 015
title: Per-channel conversation memory
status: done
priority: high
created: 2026-07-04
closed: 2026-07-05
tags: [memory, multi-turn]
related: [docs/pipeline.md, "#012"]
---

## What shipped
Server-side sliding-window chat context: last `WES_CONV_TURNS` exchanges
replayed to whichever backend answers, shared across the escalation handoff,
idle-expired after `WES_CONV_TTL`, cleared via `POST /reset_conversation`. Keyed
**per channel** (voice / discord / text) so a remote chat doesn't clobber the
in-house context. Both user and assistant turns are stored and replayed.

## Outcome
Verified e2e (fact recalled across turns through the full audio loop) and gated
by the `memory-recall` golden case; zero measured latency cost. Unblocked
multi-turn eval cases (`turns:` in golden.yaml). Durable/agentic memory is the
separate #012.
