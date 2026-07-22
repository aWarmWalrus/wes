---
id: 003
title: Speak a filler before slow tool turns
status: done
priority: med
created: 2026-07-05
closed: 2026-07-21
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

### 2026-07-21 — closed as OBSOLETE, superseded without implementing either fix
Both proposed fixes are moot:
- **Filler:** `docs/pipeline.md` ("Single-stream playback") now explicitly
  states "No filler/'thinking word' — it was removed as clunky." The one
  surviving filler (`ESCALATE_ACK`) is narrowly scoped to escalation
  spin-up, not generalized to tool turns as this ticket proposed.
- **Prefetch/TTL:** a **wake-word vision prefetch** (`docs/vision.md`,
  "Wake-word vision prefetch (latency hiding)") now fires `/prefetch_scene`
  at wake-word detection time, ahead of the user finishing their utterance,
  populating the `SCENE_TTL=20s` cache before the turn even starts. Per that
  doc: "wake-word prefetch hides it; an ad-hoc cache-miss `describe_scene` is
  slow" — i.e. the miss case this ticket targeted is now the rare path, not
  the common one, without a filler or TTL change.
Both landed via separate feature work (vision prefetch) after this ticket
was filed 2026-07-05; nothing here needs building. Closing rather than
reopening scope-creep into vision.
