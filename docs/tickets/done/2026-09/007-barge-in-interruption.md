---
id: 007
title: Barge-in — cut the reply off by speaking mid-playback
status: done
priority: low
created: 2026-07-04
closed: 2026-09-02
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

## CLOSED 2026-09-02 — obsolete, not shipped

The Raspberry Pi tier was retired: the owner repurposed the hardware, and with
it went the microphone, the speaker, the camera and the Hailo-8 accelerator that
every part of this ticket depended on. Nothing here was built. It is filed under
`done/` because that is where closed tickets live, not because it shipped.

The code it would have modified is in `archive/pi/`, and the design write-ups it
referenced moved to `docs/archive/`. If a Pi ever rejoins the system, this is
still a reasonable statement of the problem — read it with
`archive/pi/README.md` beside it.
