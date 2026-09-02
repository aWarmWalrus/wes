---
id: 005
title: Scheduled actions — timers, reminders, recurring routines
status: open
priority: med
created: 2026-07-04
closed:
tags: [scheduling, tools, audio-rule]
related: ["#002", "#004", "#016"]
---

## Goal
Timers, alarms, reminders, and recurring routines ("remind me at 3", "every
morning tell me the weather").

## Approach
- **Mechanics**: a `schedule_action` tool writing to a persisted store
  (`~/wes/schedules.json` on the Pi or a PC-side equivalent), plus a scheduler
  loop in an existing process (the Pi client already has a monitor thread) that
  fires the action through the normal reply path. One-shot + cron-style
  recurrence; list and cancel tools ("what are my reminders", "cancel that").
- **Delivery**: reuse the `POST /announce` primitive built for alerts (#021) —
  it already phrases a proactive message and records it in conversation memory.
  When the owner is away, a reminder can DM via Discord instead of speaking.

## Audio-rule interaction
A scheduled action *speaks later, unprompted*. Treat the user's explicit request
as the confirmation for that one schedule (it plays on the JBL only — Matcha &
Good gray stay off-limits), and announce-once rather than nagging. Anything WES
wants to say on its OWN initiative (e.g. "nightly eval regressed") stays
log/DM-only per the house rule.

## Acceptance
- [ ] schedule / list / cancel tools working, persisted across restarts
- [ ] one-shot and recurring both fire through the reply path
- [ ] fires via announce (voice when home, Discord when away)

## Notes
Depends on nothing hard — can precede #004; once HA exists they compose ("turn
off the lights at 11"). Would also close part of the "deferred action" gap in
#002.

## Update 2026-09-02 — delivery is Discord-only

The Pi was repurposed, so two details above are stale:

- Storage "`~/wes/schedules.json` on the Pi" — must be PC-side.
- "the Pi client already has a monitor thread" as the host for the scheduler
  loop. The nearest live equivalent is `wes_discord.py`, which already runs two
  background watchers (`alert_watch`, `fantasy_watch`) in its own process. That
  is the obvious home, with the caveat that it shares fate with the bot (#032).

The acceptance criterion "fires via announce (voice when home, Discord when
away)" collapses to just Discord, which makes this **simpler**, not blocked:
`/announce` is unchanged and is still the right primitive.
