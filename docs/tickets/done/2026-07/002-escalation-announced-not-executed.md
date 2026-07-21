---
id: 002
title: Router promises to escalate but never calls escalate_to_claude
status: done
priority: high
created: 2026-07-05
closed: 2026-07-16
tags: [router, escalation, multi-turn]
related: [5bd8cdc, tests/eval/golden.yaml, "#005", "#029"]
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
- [x] a query that triggers a defer-promise ends with either a real escalation
      or a direct answer — never a bare unkept promise
- [x] multi-turn golden case covers the follow-up
- [x] full eval green

## Notes
Related to the invisible-escalation work in `docs/pipeline.md` (ESCALATE_ACK,
buffered retraction).

### 2026-07-16 — SHIPPED
`wes_server._is_unkept_promise()` + a retry in `_think_local()`.

**The key design insight: wording alone cannot detect this.** The turn log has
`"I'll check the system status for you."` followed by a real `get_system_status`
call — a *kept* promise that must not be touched. So the guard keys off the
turn's **tool notes**, not the phrasing. It fires only when all three hold:
1. the reply matches `_PROMISE_RE` (defer/handoff language),
2. **no tool ran** this turn (a tool run means the promise was kept), and
3. no escalation already happened.

Then it re-runs at the deep tier with `NO_DEFER_FRAMING` prepended and replaces
the reply. Keeps the original if the retry returns empty — never trade a weak
answer for dead air. Also requires an escalation target to exist; with nowhere
to retry, a weak promise still beats no answer.

**Scope: buffered channels only** (`_think_local` → Discord / `/respond_text`),
where nothing has reached the user yet. Streaming voice (`/respond_stream`) is
already speaking and cannot retract — out of scope, per this ticket's
"buffered channels first".

**What proves what** (worth being precise about):
- The **unit tests** (`TestUnkeptPromiseGuard`, 8 cases) prove the guard's code
  path, including the must-not-regress case "promise + real tool call is KEPT".
- The **golden cases** (`promise-not-deferred`, `promise-not-deferred-followup`)
  prove the *outcome* — no bare unkept promises — but they pass because the
  model currently behaves, not because the guard fires. They'd likely pass
  without the guard too. They are a regression net for the behaviour; the guard
  is the safety net for when the model drifts back.
- Live spot-check: "why does the Riemann hypothesis matter" escalated correctly
  on its own (`[route] escalating to gemma4:12b`), guard dormant — correct.

Results: full unit suite **209 passed, 7 skipped**; eval **16/16**, judge
correct-avg **1.94/2** vs 1.86 recent median (above baseline).

Ran on the single-model topology (gemma4:12b as router + escalation, 2026-07-16)
— so on Discord/deep channels the retry is the same model as the router, and the
value there is the explicit anti-defer instruction rather than a tier upgrade.
