---
id: 008
title: Inject prefetched scene into context to skip the vision tool round-trip
status: open
priority: low
created: 2026-07-04
closed:
tags: [vision, latency, prompt]
related: [docs/vision.md, docs/keyresults.md, "#003"]
---

## Goal
"What do you see" currently costs two LLM calls (tool decision → tool result →
final reply). Drop to one by injecting the wake-word-prefetched scene
description directly into context (was KR3).

## Approach
`_scene_context()` already injects recognized faces into the system prompt. Do
the same for the prefetched Gemma scene description when it's fresh, so an
obvious "what do you see" can be answered without a `describe_scene` tool round-
trip. Keep the tool for on-demand / stale-cache cases.

## Acceptance
- [ ] a fresh-prefetch "what do you see" answers in one LLM call
- [ ] measured via X-Llm-Ms / timing.csv
- [ ] still correct when the prefetch is stale/absent (falls back to the tool)

## Notes
Interacts with #003 (latency) and #001 (make sure this doesn't re-open the
hallucination path — injected context must be clearly "current view", and
absence must NOT read as "empty room").
