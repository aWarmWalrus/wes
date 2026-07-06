---
id: 003
title: Speak a filler before slow tool turns
status: open
priority: med
created: 2026-07-05
closed:
tags: [latency, voice, tools, vision]
related: [docs/pipeline.md, "#001"]
---

## Problem / Goal
Voice *feels* much slower than Discord, but the baseline pipelines are
comparable (measured 2026-07-05: text turns 1.1-1.9s; voice stt 370-420ms +
ttfa 1.0-1.9s). The felt slowness is mostly physics: VAD waits ~1s of silence
before the turn starts, and the reply is consumed at speaking speed (5-15s of
audio vs instant text).

The one genuinely addressable stall is **tool turns blocking first audio**: a
`describe_scene` cache miss measured **ttfa 17.9s** (capture + face-rec + 12b
VLM). `SCENE_TTL=20s` means misses are common.

## Approach
- Speak a filler ("let me take a look") before slow tools, the way
  `ESCALATE_ACK` masks escalation latency — turns dead air into a natural beat.
- And/or lengthen `SCENE_TTL` / prefetch more aggressively so misses are rarer.
- Do NOT chase the baseline 1.4-2.3s turn time; the perf eval gate already
  tracks it and there's no quality gain to be had there.

## Acceptance
- [ ] a describe_scene-triggering voice turn produces audio within ~2s (filler),
      not ~18s of silence
- [ ] perf_check shows no regression on non-tool turns

## Notes
Filler must respect the house audio rule implicitly — it's part of the normal
reply path on the JBL, not unprompted playback.
