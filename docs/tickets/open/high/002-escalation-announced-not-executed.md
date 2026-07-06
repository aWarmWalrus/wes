---
id: 002
title: Router promises to escalate but never calls escalate_to_claude
status: open
priority: high
created: 2026-07-05
closed:
tags: [router, escalation, multi-turn]
related: [5bd8cdc, tests/eval/golden.yaml, "#005"]
---

## Problem
The router sometimes *says* "I'll ask for help / look into it" while making
**no `escalate_to_claude` call at all** — so nothing actually happens, the
promise is never kept, and the next turn inherits an unfulfilled promise it
can't act on. This is the "multi-turn" gap: WES has no deferred actions.

Distinct from the already-fixed case: the RESET/buffered-retraction fix
(commit 5bd8cdc) handles announce+call in the SAME reply. The remaining failure
is announce with **no call**, so there's nothing to retract. Seen on Discord
(e.g. the "explain the quadratic formula to a toddler" turn answered weakly
with no escalation).

## Approach
- Server-side check: if the reply matches a promise-to-escalate/defer pattern
  AND no escalation happened, re-run the turn as an escalation (mirrors the
  retraction design; buffered channels first).
- Extend the `escalation-silent` golden case with a multi-turn variant (a
  "can you look into it?" follow-up).
- Longer term, scheduled/deferred actions (#005) would make "I'll get back to
  you" genuinely possible.

## Acceptance
- [ ] a query that triggers a defer-promise ends with either a real escalation
      or a direct answer — never a bare unkept promise
- [ ] multi-turn golden case covers the follow-up
- [ ] full eval green

## Notes
Related to the invisible-escalation work in `docs/pipeline.md` (ESCALATE_ACK,
buffered retraction).
