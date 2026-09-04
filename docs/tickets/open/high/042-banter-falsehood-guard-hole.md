---
id: 042
title: unverifiable() misses fabricated pick numbers for players not yet in context
status: open
priority: high
created: 2026-09-04
closed:
tags: [banter, safety, draft, sleeper]
related: ["#039", pc/sleeper/banter.py, docs/tickets/open/med/041-qualitative-player-notes.md]
---

## Problem

`unverifiable()` is the guard that stops the draft bot posting checkable false
claims into a public room under the owner's account. It has a hole, and the
hole was found in production.

**2026-09-04, live 12-team mock.** The bot posted, twice:

> "Puka at 5 feels like a bit of a reach, honestly."
> "Woah, hold on, Puka at 5 is a bit aggressive, isn't it?"

Pick 5 was **Ja'Marr Chase**. Puka Nacua went at **pick 10**. Both lines were
posted, i.e. both passed the guard. The payload handed to the model contained
the correct picks on both occasions — the second one literally listed
`10:Puka Nacua`.

The guard DOES catch this shape when it can see the player: a week earlier it
dropped "says Mark Andrews went at 13; he went at 125" (recorded as
`draft.banter.dropped`). So the failure is conditional, and the likely
condition is the first occurrence: at 13:18 the context's `recent_picks`
covered picks 1-6 and Puka had not been drafted at all. A player the guard has
no record of appears to fall through as "cannot check" rather than "must not
claim" — and then the same line recurred once he HAD been picked.

## Why this is high priority

It is the only output in this system that goes out in public, under the
owner's name, unprompted, about other people's picks. Everything else the agent
does is either private (the ledger) or reversible (a lineup change). A
confidently wrong pick number cannot be walked back, and the whole design
argument for letting a model post at all is that the guard makes a false claim
impossible rather than unlikely.

It also invalidates a reasoning step used elsewhere: the guard was cited as the
reason a weaker/cheaper model could be trusted with chat. It cannot carry that
weight until this is closed.

## Approach

1. Reproduce directly: a line naming a player absent from `recent_picks` and a
   line naming one present but at the wrong number. Both must be dropped.
2. Decide the default for the unknown case. **Fail closed**: if a line asserts
   a pick number for a player the context cannot confirm, drop it. Silence is
   free; the room does not notice a message that was never sent.
3. Check the same asymmetry for the other checkable assertions (ownership,
   round, "we wanted him", verdicts).

## Acceptance

- [ ] a claimed pick number for a player NOT in context is dropped
- [ ] a claimed pick number that contradicts context is dropped (regression:
      the "Puka at 5" payloads are in the draft log; replay them)
- [ ] the `draft.banter.dropped` record names the reason, as it already does
- [ ] a true claim is still posted (the guard must not silence everything)

## Notes

Found while reverting banter from `gemma3:4b` to `gemma4:12b`. The small model
produced these lines, but this is NOT a small-model bug: the guard exists
precisely so the model's reliability is not the thing standing between a
fabrication and a public room. A smaller model finds the hole more often; the
hole is the defect.
