---
id: 013
title: Storage tier on the PC for audio/video/transcripts
status: open
priority: low
created: 2026-07-04
closed:
tags: [storage, infra]
related: []
---

## Goal
A durable storage tier on the PC for audio/video/transcripts (currently only
transient captures + the size-capped turn log exist).

## Approach
Undesigned. Decide retention, format, and privacy stance first — this stores
household content (see the turn-log retention reasoning: capped rolling window,
not append-forever). Consider what actually needs keeping vs. what the turn log
already covers.

## Acceptance
- [ ] retention + privacy policy decided before any capture-to-disk lands

## Notes
Low priority; note the disk-growth/retention audit findings (Pi journald 3.1GB,
no rotation on some logs) — don't add an unbounded consumer.
