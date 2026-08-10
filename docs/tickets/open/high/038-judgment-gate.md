---
id: 038
title: Judgment gate — let the LLM veto roster moves, in one voice
status: open
priority: high
created: 2026-08-09
closed:
tags: [fantasy, nfl, llm, safety]
related: ["#029", "#035", "#036", "#037", docs/data-architecture.md]
---

## Problem / Goal

Every roster decision today is **strictly numeric**. Fantasy football has real
judgment in it that numbers don't carry, and the system currently has no way to
express any of it.

Owner, 2026-08-08, with two examples:

1. *"I don't want to have too many players from the same team."*
2. *"Maybe Yahoo hasn't properly adjusted for the fact that a player is out, so
   the bench player will be getting more minutes."*

**These are different in kind, and only one of them wants an LLM.**

**(1) is a constraint, not judgment.** You can count teams. The reason it
matters is correlation — if one offence gets shut out you lose three starters at
once — and the threshold is taste. That is config:

```yaml
max_players_per_nfl_team: 3
```

Deterministic, testable, auditable, cannot hallucinate. Encoding it is strictly
better than asking a model about it every week.

**(2) is mostly a DATA gap, with a judgment residue.** It decomposes into:

- *Is the starter out?* — ESPN injury designations (verified working, #036).
- *Who backs him up?* — nflverse publishes `depth_charts` (#036).
- *Has the projection caught up?* — compare projection freshness to the news.

Three mechanical checks. What is left after them is the genuine judgment: will
this back actually get the work, or is it a committee? That is beat-reporter
territory, and it is where a model adds something arithmetic cannot.

**So the goal is narrow on purpose**: capture the residue, after the data gap is
closed by #036 and the constraint by config. Reaching for an LLM to solve what
is really missing data would produce a system that guesses at facts it could
have looked up.

## Approach

### The safety property: the model may SUBTRACT, never ADD

```
layer 4 (deterministic)  ->  candidate move + its numeric reason
layer 5 (LLM)            ->  may VETO or ANNOTATE.
                             May NOT originate a move. May NOT change a number.
```

This asymmetry is the whole design. A model that can block a drop can only cause
**inaction** — the failure mode this system already prefers everywhere (guard-
rails fail closed, `value: None` refuses, an unreadable ledger degrades). A model
that can *originate* a drop turns a hallucination into an irreversible write on a
real account. Same reasoning as `approve={drop,add}` being re-checked against the
live recommendation: model output is a claim to verify, not an instruction.

Three permitted roles:

1. **Veto → route to human.** Blocks the unattended path and DMs instead.
2. **Annotate.** Attach a judgment note without changing the decision.
3. **Propose a constraint** the OWNER ratifies into config. The model notices the
   Bengals stack; the owner decides whether `max_players_per_nfl_team: 3` is
   their rule. Judgment gets encoded once instead of re-litigated weekly.

### Use the escalation path, not the local 12b

Weighing ambiguous situational context is the task `gemma4:12b` is worst at. A
weak model here does not add judgment, it adds variance — and variance on a veto
path either cries wolf until the owner ignores it, or vetoes nothing. Route
through `escalate_hard` (#026 already sizes its budget).

### One voice — the model must not narrate its own plumbing

Owner requirement, 2026-08-09: *"I want the model to feel like a single brain...
it shouldn't engage with me like 'the regression model wanted to do this, but I
said no'."*

**Audit trail and voice are different surfaces, and separating them costs
nothing:**

- **Ledger** — structured and explicit: `moves`, `why`, `veto`, `veto_reason`,
  each separable, so "was the model right the twelve times it overrode the
  numbers?" stays answerable in December.
- **DM / speech** — one paragraph, one agent, no pipeline.

This is already the house pattern: `wes_discord.fantasy_watch` hands the ledger
entry to Jarvis via `/announce` to phrase, with `raw_fantasy_summary` only as a
fallback. The veto becomes another fact in the entry, not a second voice.

**But a unified voice is not a flattened one.** A good human GM never says "my
spreadsheet says", yet you can still hear which part is computed:

> *"Strange is worth about four more points a week, but I'd hold — Jefferson's
> out and Addison should see more targets."*

One brain; the four points still reads as a number and the hold as a read. The
failure to avoid is not sounding mechanical, it is sounding **equally certain
about everything** — which makes the veto useless, because the owner cannot tell
when to look closer.

### Known exposure: number drift

`why` lines are computed deterministically so the explanation matches what
actually happened. Handing them to a model to rephrase lets it drift — "+6" when
the ledger says +4.4, or the wrong player named. **This risk already exists on
the current DM path**; the veto only adds surface. Rule: the model phrases, the
numbers come from the structured entry — and given the model, that wants
checking rather than trusting.

## Acceptance

- [ ] `max_players_per_nfl_team` (and any similar taste-constraints) enforced
      deterministically in the decision layer — **not** delegated to the model
- [ ] Judgment gate can VETO or ANNOTATE a candidate move; it cannot originate
      one or alter a value (test-enforced — this is the safety property)
- [ ] A veto blocks the **unattended** path and routes to the owner via Discord
- [ ] A veto **never blocks a move the owner explicitly approved** — it warns
      once, in the same message, and the move proceeds (the human has the final
      say; re-asking after "do it" is not taking the answer)
- [ ] Veto reasoning is natural language, recorded in the ledger as its own
      field, and never surfaced as a separate voice in the message
- [ ] User-facing output reads as one agent; hedging conveys confidence without
      exposing the pipeline
- [ ] Numbers in any phrased message match the structured entry
- [ ] The offline fallback (no phrasing model) still reads as one voice — a
      fallback that says "veto applied" breaks the illusion exactly when
      something is already wrong
- [ ] Judgment runs on the escalation model, not the local 12b
- [ ] Vetoes are reviewable after the fact: enough logged to judge whether the
      gate is contributing signal or noise

## Notes

**2026-08-09 — sequencing.** Depends on #036 for the injury/depth-chart data,
because most of example (2) is a data gap. Building the gate first would have it
guessing at facts it could look up. It is **not** an adjuster under #037 —
adjusters are pure, numeric and bounded, and this is none of those; it is a
distinct gate between layers 4 and 5, which keeps the regression layer testable
by arithmetic.

**RESOLVED 2026-08-09 — the human has the final say.** A veto blocks only the
UNATTENDED path. Against a move the owner explicitly approved, the gate may
warn but the move proceeds.

Declining to act alone and countermanding a direct instruction are different
kinds of authority, and only the first is delegated here. A model overriding an
explicit human decision is a worse failure than a missed edge — especially given
the model is sometimes a 12b and its judgment is exactly the part that cannot be
verified by arithmetic.

Two consequences to build to:

- **Warn once, then act.** The caution rides along with the move; it does not
  become a confirmation prompt. Re-asking after an explicit "do it" is a way of
  not taking the answer, and it trains the owner to click through warnings.
- **The warning must be visible BEFORE the write where that's possible.** If the
  approval and execution are in the same turn, say the concern in the same
  message that reports the move — a caution arriving after an irreversible drop
  is worth nothing.

The case this deliberately does not cover: the model learning something the
owner could not have known at approval time (a starter ruled out minutes
earlier). If that turns out to matter, the answer is a narrow set of
hard-checkable facts that block anything — which is a *constraint*, belongs with
`never_drop`, and must not be smuggled in here as judgment.

**Status: DESIGN ONLY — not approved to build.** No code written.
