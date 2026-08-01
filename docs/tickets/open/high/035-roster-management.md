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
