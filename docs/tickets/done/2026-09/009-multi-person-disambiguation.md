---
id: 009
title: Verify clothing-color disambiguation with 3+ people in frame
status: done
priority: low
created: 2026-07-04
closed: 2026-09-02
tags: [vision, face-rec]
related: [docs/vision.md, docs/keyresults.md]
---

## Goal
Clothing-color disambiguation (`docs/vision.md`) is verified with the current
2-person set (charlie/cindy). Confirm it stays correct with 3+ people in frame
at once (was KR4).

## Acceptance
- [ ] face-rec + clothing tags stay correctly matched with 3+ people present
- [ ] "who's here?" names them all without swapping identities/clothing

## Notes
Mostly a verification/tuning task; may surface ArcFace threshold or clothing-tag
edge cases that need fixing.

## CLOSED 2026-09-02 — obsolete, not shipped

The Raspberry Pi tier was retired: the owner repurposed the hardware, and with
it went the microphone, the speaker, the camera and the Hailo-8 accelerator that
every part of this ticket depended on. Nothing here was built. It is filed under
`done/` because that is where closed tickets live, not because it shipped.

The code it would have modified is in `archive/pi/`, and the design write-ups it
referenced moved to `docs/archive/`. If a Pi ever rejoins the system, this is
still a reasonable statement of the problem — read it with
`archive/pi/README.md` beside it.
