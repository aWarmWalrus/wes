---
id: 008
title: Inject prefetched scene into context to skip the vision tool round-trip
status: done
priority: low
created: 2026-07-04
closed: 2026-09-02
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

## CLOSED 2026-09-02 — obsolete, not shipped

The Raspberry Pi tier was retired: the owner repurposed the hardware, and with
it went the microphone, the speaker, the camera and the Hailo-8 accelerator that
every part of this ticket depended on. Nothing here was built. It is filed under
`done/` because that is where closed tickets live, not because it shipped.

The code it would have modified is in `archive/pi/`, and the design write-ups it
referenced moved to `docs/archive/`. If a Pi ever rejoins the system, this is
still a reasonable statement of the problem — read it with
`archive/pi/README.md` beside it.
