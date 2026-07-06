---
id: 014
title: Fully-local inference — e4b router + 12b escalation with thinking
status: done
priority: high
created: 2026-07-04
closed: 2026-07-05
tags: [llm, routing, local]
related: [docs/pipeline.md]
---

## What shipped
`WES_LLM=local` runs **gemma4:e4b** via Ollama as the primary router (streaming
+ tools + vision, one resident model for chat and `describe_scene`). Smart
routing (`WES_ESCALATE`): e4b carries an `escalate_to_claude` function and hands
hard queries to the resident **gemma4:12b with thinking** (`WES_ESCALATE_MODEL`)
— fully local since 2026-07-05. Claude Haiku is now error-fallback only. A
server-injected ack (`WES_ESCALATE_ACK`) masks the thinking latency on voice;
escalation is invisible (buffered retraction, commit 5bd8cdc).

## Outcome
Measured llm ~706ms vs Haiku ~1305ms; total turn ~2.6s vs ~4.0s. Keeping e4b as
router is evidence-backed (A/B in docs/pipeline.md: 12b-as-router = +44-63%
latency, no quality gain). Remaining follow-ups spun out to #002.
