---
id: 009
title: Verify clothing-color disambiguation with 3+ people in frame
status: open
priority: low
created: 2026-07-04
closed:
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
