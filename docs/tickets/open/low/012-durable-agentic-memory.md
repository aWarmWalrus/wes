---
id: 012
title: Durable agentic memory — semantic/episodic + nightly consolidation
status: open
priority: low
created: 2026-07-04
closed:
tags: [memory, agentic]
related: [docs/memory-design.md, "#015"]
---

## Goal
Turn-level conversation memory is DONE (#015). The bigger step: durable agentic
memory — semantic facts, episodic day logs, nightly consolidation — toward a
broader OpenClaw-like Jarvis.

## Approach
Designed in **`docs/memory-design.md`** (2026-07-04): file-based `MEMORY.md`
approach chosen over vector/Letta/Mem0 infra, with `remember`/`forget` tools and
nightly consolidation. Build order is in that doc.

## Acceptance
- [ ] remember/forget tools persist facts across sessions
- [ ] nightly consolidation summarizes episodic logs

## Notes
Not started. Read `docs/memory-design.md` before building — the design decision
(file-based over vector DB) is already made and justified there.
