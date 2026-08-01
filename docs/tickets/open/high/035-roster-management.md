---
id: 035
title: Roster management — recent-form drops + free-agent pickups (broadens waiver_claim)
status: open
priority: high
created: 2026-07-31
closed:
tags: [nfl, fantasy, yahoo, roster, waivers, rails, agentic, actions]
related: ["#029", "#034", docs/fantasy-gm-design.md, docs/data-architecture.md]
---

## Problem / Goal

Owner: *"let's broaden the scope of waiver claim to manage roster since it
doesn't have to just be waiver moves. it should be robust enough to drop players
who are underperforming in recent weeks and see if anyone is worth picking up
instead."*

Today the GM can only rearrange players it already has. `set_lineup` is the sole
implemented action; the roster itself is fixed. This ticket makes the roster
itself managed: evaluate who's underperforming **recently**, check whether the
free-agent pool has someone better, and (gated) swap them.

Supersedes the narrower `waiver_claim` framing in #029's `actions_allowed` —
waiver claims are one mechanism, not the goal.

## THE RISK THAT MAKES THIS DIFFERENT

**Every action shipped so far is reversible. This one is not.** A bad
`set_lineup` is corrected by the next scheduled run, at zero cost. **A dropped
player can be claimed by another manager within minutes and is gone
permanently** — no guardrail un-drops them.

That asymmetry, not the code volume, is what should drive the phasing. Note also
that #029's `auto` justification ("the owner truly doesn't care about that
league's outcome") was reasoned about *reversible* actions; "doesn't care about
the outcome" and "fine with irreversible moves" are not the same claim, and the
owner should re-affirm the second one explicitly before any drop executes.

## Prerequisites, both PROVEN by recon (2026-07-31)

**1. Recent-form data — SOLVED.** Everything in the engine today is a season
aggregate, which cannot express "underperforming lately". ESPN's per-athlete
**`gamelog`** endpoint returns real per-game stat lines:

```
.../football/nfl/athletes/{id}/gamelog
  labels: [receptions, receivingTargets, receivingYards, ..., fumblesLost]
  events: [{eventId, stats: ['4','10','27','6.8','0', ...]}, ...]
```

Those labels map onto the SAME canonical keys `wes_nfl._ESPN_LABEL` already
uses, so each game can be scored with the existing `fantasy_points` under the
league's real weights. Recent form = score the last N games; compare to the
season baseline already computed.

**2. Free-agent pool — SOLVED.** `/f1/<league>/players` has `#players-table`
(25 rows/page) with everything needed, in the same DOM shapes the roster scraper
already parses (`a.name`, `span.Fz-xxs`):

```
Parker Washington   detail='Jax - WR'  cells=[..., 'FA', bye, ..., pts, rank]
Stefon Diggs        detail='NA'        cells=[..., 'FA', ...]
```

A **Roster Status** column distinguishes `FA` (free pickup) from waivers, which
decides whether an add is immediate or a claim.

## Layers affected (per docs/data-architecture.md)

| Layer | Change |
|---|---|
| 1 raw | **none** — `wes_http` covers gamelog; Yahoo stays on the browser path |
| 2 fantasy data | **new** — `parse_gamelog` (per-game lines), `free_agents()` (scrape `#players-table`) |
| 3 regression | **new** — `recent_form()`: score last N games, compare to baseline |
| 4 decision | **new** — `recommend_roster_moves()`: pure, pairs a drop candidate with an add candidate |
| 5 model | **extend** — a tool + the WHY summary must explain drops in the same voice as lineup moves |

Plus `wes_execute`: a **second write path** (add/drop), which needs its own DOM
recon — the add/drop flow is NOT the lineup swapper.

## Approach — phased by RISK, not by effort

**R1 — Recent form, read-only.** `parse_gamelog` + `recent_form()`. Surfaces
"who on my roster has fallen off" with no write path at all. Immediately useful
on its own, and it makes the valuation honest about time, which also unblocks
the matchup/projection work #029 listed.

**R2 — Free-agent visibility, read-only.** `free_agents()` + rank them with the
existing valuer. Answers "is anyone better available?" — still zero writes.
**R1+R2 together deliver the entire ANALYSIS the owner asked for**, with none of
the irreversibility.

**R3 — Recommendation engine, pure.** `recommend_roster_moves()`: pair the worst
recent performer against the best available, subject to positional need. Pure
and unit-testable, exactly like `optimize_lineup`/`_plan_swaps`.

**R4 — Guardrail enforcement. MUST land before any write.** `never_drop`,
`max_moves_per_week`, `max_faab_bid_pct` are declared in `teams.yaml` and read
by **NO code** today (verified). They are harmless while `set_lineup` is the
only action; they become load-bearing the moment a drop is possible. Also needs
a real `max_moves_per_week` counter, which means reading the ledger — the first
guardrail that depends on history rather than config.

**R5 — Add/drop execution, gated.** DOM recon of Yahoo's add/drop flow, then a
write path with the same discipline as `_submit_lineup` (act, re-read, verify).
**Default to `propose` even on the auto team** — the one place I'd not inherit
`auto` automatically, because of irreversibility.

**R6 — IL/IR usage.** Once a roster spot can be freed, stashing a long-term
injury on IR finally pays off (#029 found the optimizer never targets IR).

## Acceptance

- [ ] `recent_form` scores the last N games from real gamelog data, per league scoring
- [ ] A player's recent form can be compared against their own season baseline
- [ ] `free_agents()` returns the available pool with position, team, bye, FA-vs-waivers
- [ ] `recommend_roster_moves()` is pure and property-tested
- [ ] `never_drop` / `max_moves_per_week` / `max_faab_bid_pct` are ENFORCED, with tests
- [ ] `max_moves_per_week` counts real executed moves from the ledger
- [ ] No drop can execute without an explicit owner-confirmed gate
- [ ] Every drop/add appears in the ledger and the DM, explained in plain language

## Notes

### 2026-07-31 — R2-R6 ALL SHIPPED
- **R2** `wes_yahoo.free_agents()` — scrapes `#players-table`. Yahoo's default
  view is already the available pool, best-first (verified: `count=`/`status=`
  params do NOT change page size, so this reads the default rather than
  pretending to control it). Anything whose status isn't exactly `FA` is treated
  as NOT a free pickup — guessing an unknown waiver marker means "just add them"
  turns a claim into a failed action.
- **R3** `recommend_roster_moves()` — pure. Same-position only (dropping the
  only kicker for a 4th WR is strictly worse regardless of points); UNKNOWN form
  or UNKNOWN value never justifies a drop; a `min_gain` floor so the roster
  isn't churned for +0.3.
- **R4** guardrails ENFORCED at last: `never_drop` (loose name match so
  punctuation can't defeat protection) and `max_moves_per_week`, counted from
  the **ledger** — the first guardrail depending on history rather than config.
  An unreadable ledger **fails closed**: assuming zero moves would silently
  allow unlimited drops whenever the ledger breaks.
- **R5** `_submit_add_drop()` — recon found `/addplayer?apid=<id>` opens a form
  (GET, commits nothing — verified by re-reading the roster) which POSTs to
  `/f1/<league>/<team>/addplayer` with `apid` + a `dpid` checkbox, both being
  player keys `roster_players` already returns. Verifies BOTH the add and the
  drop landed before reporting success.
- **R6** `il_candidates()` — surfaces injured players who could be stashed on
  IR to free a bench spot. Only meaningful now that a freed spot can be filled.
  `Q` deliberately doesn't qualify (those players routinely play).

**The safety property that matters most:** `propose_roster_moves(execute=False)`
by default **even for an `auto` team**. Lineup changes inherit autonomy; drops
do not, because they're irreversible. A test pins exactly that, and the tool
dispatch requires `execute is True` — a stringy `"true"` degrades to
recommending, not to dropping someone.

**Verified live, read-only, roster confirmed unchanged**, through a real turn:
> "Drop Jordan Addison (WR, 6.25 pts in recent games, down from 8.15 on the
> season) for Parker Washington (8.98) — about +2.73 points."

704 tests pass (was 648). Still open: waiver CLAIMS (as opposed to free-agent
adds) aren't implemented — non-FA players are skipped rather than claimed; and
`max_faab_bid_pct` stays unenforced because nothing spends FAAB yet.

- **Don't reuse `_plan_swaps`.** A lineup swap trades two slots within a fixed
  roster; add/drop changes roster membership. Superficially similar, different
  invariants — the kind of resemblance that invites a subtle bug.
- **Recent form needs a games-played floor.** One good game off injury isn't
  "form"; a 2-game sample will rank noise above signal. Needs an explicit
  minimum and an honest UNKNOWN below it (the `value: None` convention, not 0).
- **Bye weeks matter for adds** in a way they don't for lineups: picking up a
  player on bye is often correct (they play next week), so the bye check that
  benches someone must NOT disqualify them from being added.
- ESPN gamelog is **per-athlete**, i.e. one HTTP call per player. A 15-man
  roster is fine; the whole FA pool is not. Rank FAs on season stats first, then
  pull gamelogs only for the shortlist.

### 2026-07-31 — PROACTIVE suggestions: the scheduled run now recommends, unasked
Owner: *"can jarvis at least recommend a roster move? like, automatically check
if anyone is underperforming and recommend a change without my prompting?"*

The pieces existed but weren't wired: the scheduled runner only ran the LINEUP
check, and the Discord watcher deliberately ignored anything that wasn't a real
write. Both now handle recommendations.

- `fantasy_gm_run` runs the roster check after the lineup check, per team, with
  **`execute=False` always — regardless of autonomy**. The scheduled job may
  suggest a drop but never make one; that stays an explicit ask (#035's
  irreversibility rule). A crash in one check no longer costs the other.
- `fantasy_watch` DMs suggestions as well as writes, with **distinct framing**:
  "NOTHING HAS BEEN DONE — this is a suggestion only", so a proposal can never
  read as a completed action.

**The design problem, and a wrong turn worth recording.** The job fires daily, so
an unchanged suggestion would re-send "drop Addison for Washington" every
morning — the alert-fatigue failure `alert_watch` avoids by notifying on state
CHANGE, not every poll. First attempt derived the dedup from the ledger, which
*looked* more robust (survives restarts). It was wrong: eight identical rows from
development runs — written before the notifier existed — silenced a suggestion
the owner had **never actually been told about**. Observed live: the run
completed, the ledger had the entry, and no DM ever came.

Moved the dedup **in-memory into the watcher**, matching `alert_watch` exactly
(which also re-notifies current state after a restart). Re-notifying once after a
restart is the cheaper failure than never notifying at all. Three fetch-layer
tests had to be retargeted to the watcher layer, and the test harness itself had
a bug (the loop counter doubled as the batch index, so only the first poll ever
ran) — both fixed rather than worked around.

**Verified live, twice.** First run → a real unprompted DM:
> "The system suggests swapping Jordan Addison for Parker Washington to gain
> about 2.73 projected points. No changes were made automatically since dropping
> a player is permanent, so just let me know if you'd like me to go ahead."

Second identical run → **still exactly 1 DM**. The nag guard holds.

718 tests pass (was 704).

### 2026-07-31 — the owner approved a move and it silently did nothing. Root cause.
Owner OK'd the suggestion in Discord ("yeah let's do it"). Jarvis did everything
right — called `fantasy_roster_moves({'execute': True, 'team': "Charles's Pop"})`
— and then reported there were no moves recommended. **Nothing was dropped; the
roster was verified intact.** But an approved action turning into a silent no-op
is a bad failure, so this is the root cause.

**A 200-with-no-data was being CACHED.** ESPN intermittently answers a paginated
`byathlete` request with HTTP 200 and no `athletes` key. That parses fine, so
`wes_http` stored it for the full 900s `SEASON_TTL`. Consequences, in order of
how badly they hid the problem:
1. `_paginated_pool`'s per-page retry became **dead code** — every retry re-read
   the cached empty body instead of asking ESPN again. That is why the log said
   "page 2 ... never returned athletes after retry" on literally every run.
2. A degraded pool (missing WRs/TEs) was pinned for 15 minutes, so a player's
   value came back `None`, so `recommend_roster_moves` correctly refused to
   propose a drop based on unknown data — and the recommendation *vanished*
   between the suggestion and the approval.

Fix: `wes_http.get_json` takes an optional `cacheable(payload)` veto, and
`wes_nfl` passes `_has_payload` so an empty answer is returned but never
remembered. Confirmed by a regression test that the retry now **reaches the
network** and recovers.

**Immediately visible improvement**: the very next run found the pool more
complete and surfaced a BETTER move it had been missing — *Drop Jake Ferguson
(3.52 recent vs 8.54 season) for Brenton Strange (7.92), +4.4* — alongside the
original Addison one. The cache bug had been materially degrading every
recommendation, not just this one.

Second fix, from the same incident: an `execute=True` request that finds nothing
now **writes a ledger row** (`"execute requested but no move qualified"`) and
says so in the reply. Previously it returned early before any logging, so an
owner-approved no-op left no trace at all — which is exactly what made this take
a root-cause hunt instead of a log read.

722 tests pass (was 718).

## First live add/drop — 2026-08-01

The owner approved the Ferguson → Strange swap and it **executed for real**:
roster verified after at 15 players, Jake Ferguson gone, Brenton Strange added.
This is the first irreversible roster write the system has made. Two bugs had to
be fixed to get there, both worth recording.

**1. The `dpid` drop checkbox is HIDDEN.** `_submit_add_drop` did
`box.check()` on `input[name='dpid'][value=...]` and died with
`TimeoutError: element is not visible`. (No write happened — the failure was
before submit, confirmed by re-reading the roster.) Yahoo styles a real button
over the input:

```html
<button data-check-box-value="34085 " type="button"
        class="add-drop-trigger-btn ..." title="Click to drop this player">—</button>
<input type="checkbox" name="dpid" value="34085" id="checkbox-34085">
```

Fix: find the `.add-drop-trigger-btn` whose `data-check-box-value` **stripped**
equals the player key (note the trailing space in the attribute), scroll it into
view, and click it. Same lesson as the lineup swapper — drive the visible
control so Yahoo's own JS stays in the loop. Submit-button text is `"—"` for
every control in that form, so it is useless for identification; the handler now
tries a short list of submit selectors and tolerates the trigger completing the
transaction on its own.

**2. The report and the ledger described moves that never happened.** Only
`recs[0]` is ever submitted, but `body`/`why`/`entry["moves"]` were built from
the **full** rec list. So the first successful run announced *"Made this roster
move"* over two moves and wrote both to the ledger as `executed: true`, when the
Addison → Washington pair was never touched. Caught by diffing the real roster
against the report — the same independent post-write verification that caught
the wrong-player swap during development, earning its keep a second time.

Fix: in execute mode the summary and the ledger entry are rebuilt from `[top]`
alone; remaining recommendations are still shown under an explicit
`Also worth considering (not done):` heading. The bad ledger row was **not
rewritten** — a correction row was appended carrying `correction_of_ts`, because
an audit trail you can edit isn't one.

`add_drop` was also added to the team's `actions_allowed` in the PC-local
`teams.yaml`, named separately from `waiver_claim` on purpose: this is the
irreversible one and must never be granted as a side effect of allowing waivers.

723 tests pass (was 722).

## Per-action autonomy + approval-by-name — 2026-08-01

Owner review of the API: *"it seems weird to use execute as a param for that. if
it's in recommend only mode it should just recommend right?"* Correct, and the
digging found the actual cause rather than a naming nit.

**`autonomy` and `execute` overlapped, and one did nothing.** `check_guardrails`
only ever rejected `advise`, so for roster moves `propose` and `auto` were
*identical* — the mode config could not express "run my lineups but ask before
dropping anyone", and the whole decision rode on the `execute` flag. Meanwhile
for lineups `auto` genuinely did mean auto-execute. One word, two meanings,
depending on the action.

Root cause: **autonomy was per TEAM when risk is per ACTION.** A bad lineup
self-corrects on the next run; a drop never does. `autonomy` now accepts a
per-action map (scalar still means "all actions"), resolved by
`wes_execute.autonomy_for`. An action absent from the map is `advise` — not
mentioning something has not granted it.

**`execute: bool` → `approve: {drop, add}`.** The flag's real fault wasn't
ergonomics, it was carrying no identity: `execute=True` submitted whatever
`recs[0]` happened to be *at that moment*, and recs[0] demonstrably shifts (the
cache bug above did exactly that between a suggestion and its approval). An
approval now names both players and is re-checked against the live
recommendation; if it no longer matches, it **refuses and re-asks** rather than
dropping whoever is top of the list. The guard that had to be hand-written in
`do_swap.py` is now the contract — a safety check living in a scratchpad script
was the tell that the signature was wrong.

This also improves the tool layer, where a 12b decides. Echoing the two names it
heard is better-conditioned than emitting a bare boolean, and unlike a boolean a
wrong answer is *checkable*. Malformed, half-filled, or stringy values all
degrade to recommend-only.

**Charles's Pop is now TRUE full auto for add/drop** — the scheduled GM run will
drop and add players unattended and permanently. Deliberate, and confined to the
designated soak league. Two consequences worth stating plainly:
- `max_moves_per_week` **cut 6 → 3**. For lineups a cap is tidiness; for
  unattended drops it is the only bound on a bug, and on a daily schedule 6 let
  a broken run churn most of a week before the DMs made it obvious.
- Full auto has **no second look at the data**. In propose mode, the
  approval-match check is what would catch a degraded pool. Unattended, that
  defense is gone, leaving only `recommend_roster_moves` refusing on `None`
  values and `require_fresh_data_minutes`.

Note the scalar form now grants more than it used to: `autonomy: auto` means
auto for *every* action including drops. `actions_allowed` is the second key —
verified on the live config that only Charles's Pop resolves to unattended
drops.

740 tests pass (was 723).
