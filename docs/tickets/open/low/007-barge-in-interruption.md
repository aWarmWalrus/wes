---
id: 007
title: Barge-in — cut the reply off by speaking mid-playback
status: open
priority: low
created: 2026-07-04
closed:
tags: [voice, ux, vad]
related: [docs/audio.md, docs/keyresults.md]
---

## Goal
Let the user interrupt a spoken reply by talking (LiveKit-inspired). Highest-
value remaining voice UX gap (was KR2 in the last cycle).

## Approach
Keep VAD/wake-word live during `play_turn` and kill the stream on detected
speech. The A2DP persistent-silence transport fix (`docs/audio.md`) keeps the
Bluetooth channel open, which makes this feasible. The `INTERRUPT_TAG` +
`record_spoken_turn` plumbing already records a cut-off reply with the right
tag, so the conversation-memory side is partly done.

## Acceptance
- [ ] speaking during a reply stops audio within one turn cycle, no dead air
- [ ] the interrupted (partial) reply is what's remembered, tagged
- [ ] no false triggers from the reply's own audio bleeding into the mic

## Notes
Watch echo/self-trigger: the JBL output must not re-trigger the Pi's mic VAD.
