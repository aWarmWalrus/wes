---
id: 037
title: Regression strategy interface — pluggable valuers and adjusters
status: open
priority: high
created: 2026-08-08
closed:
tags: [fantasy, data, regression, architecture]
related: ["#034", "#036", "#029", "#030", docs/data-architecture.md]
---

## Problem / Goal

Layer 3 (regression) already has a contract — `{**stat_line, "value": float|None}`
— and two implementations. What it does not have is a way to **select** or
**compose** them, so every caller hardcodes one:

```python
rank_by_points(pool, scoring, tiers)   # NFL, ABSOLUTE
rank_by_zscore(pool, categories)       # NBA, RELATIVE to the pool passed in
```

`rank_by_points`'s own docstring says it "mirrors `rank_by_zscore`'s contract so
the optimizer and the draft recommender consume either sport identically". The
interface is real; it is just unnamed, unenforced, and impossible to extend
without editing every caller.

Owner goal (2026-08-08): be able to write **local, custom valuation strategies**
— our own z-score, or a Yahoo-projection baseline plus adjustments tuned for
week-to-week matchup swings — and swap between them without touching the
optimizer, the executor, or the draft tool.

### The latent bug this must fix, not inherit

**A z-score is meaningful only relative to the population it was computed over.
Points are absolute.** That asymmetry is documented in `rank_by_points` and
enforced nowhere.

`recommend_roster_moves` compares a ROSTERED player's value against a FREE
AGENT's. Today that is safe only because NFL happens to use an absolute valuer.
Swap in any relative strategy and the comparison becomes silently meaningless —
and it feeds `add_drop: auto`, which makes irreversible drops unattended (#035).

A plugin system that ignores this would take a latent bug and make it reachable
by configuration. So `scale` is part of the interface, and the decision layer
must refuse to compare relative values across populations.

## Approach

**Two roles, not one.** "Yahoo as a base plus a bunch of other things" is
naturally a producer and a chain of modifiers:

```python
# Valuer: stat lines -> values. Pure.
def value(pool: list[dict], ctx: Context) -> list[dict]     # adds value, value_parts

# Adjuster: values -> adjusted values. Pure, and BOUNDED.
def adjust(valued: list[dict], ctx: Context) -> list[dict]
```

Registries carry the metadata callers need in order to use a strategy safely:

```python
VALUERS = {
  "nfl_points": {"fn": ..., "scale": "absolute", "sports": ["nfl"]},
  "nba_zscore": {"fn": ..., "scale": "relative", "sports": ["nba"]},
  "yahoo_proj": {"fn": ..., "scale": "absolute", "sports": ["nfl"]},
  "ensemble":   {"fn": ..., "scale": "absolute", "of": [...]},
}
ADJUSTERS = {"volume_trend": ..., "dvp": ..., "home_away": ...}
```

### Why each piece

**One `ctx` replaces the ad-hoc kwargs.** `scoring`/`tiers` vs `categories` is
precisely why the two rankers aren't interchangeable today. `ctx` carries league
scoring, week, opponent map, and anything a future strategy needs.

**`ctx` is PLAIN DATA, assembled by the caller.** This is the rule that keeps
layer 3 pure (`docs/data-architecture.md`: no network, no clock, no I/O). A
strategy that fetches its own inputs stops being swappable, deterministic, or
testable by arithmetic — and purity is the whole reason this layer is cheap to
experiment in. A Yahoo-projection valuer therefore receives already-scraped
projections in `ctx`; it does not scrape.

**`value_parts` gives provenance.** `rank_by_zscore` already returns `zs` per
category; generalising it lets the WHY summary say "Yahoo 11.2, +1.2 volume
trend" instead of an unexplained number. #036 needs this for the Discord DM,
and it is what makes a custom model debuggable at all.

**Bounding becomes structural.** #036 wants adjustments capped (±15%) with the
baseline stored separately. As an Adjuster property that is enforced once, not
re-remembered per adjuster.

### Selection is CONFIG, not a model decision

Strategy is chosen per league in `teams.yaml`, not by the LLM per query. If the
model picks, two runs value the same player differently and the action ledger
stops being interpretable — fatal for a system that already writes
irreversibly. The agent may *report* the active strategy and run explicit A/B
comparisons; it may not silently switch.

### Package boundary now, separate repo later

Owner is considering a separate repo for this layer. **Recommendation: create
the package boundary now, defer the split.**

The split genuinely buys hard enforcement, independent CI, and freedom to
iterate on models without touching a live assistant. But layer 3 needs the
league-scoring contract (produced by the Yahoo scrape) and player identity
mapping; splitting today forces both to be published as stable external
contracts *before* we know what the models need from them. There is also real
multi-location friction — 2026-08-08 added a second clone, and a third repo
multiplies it.

So: build it in `pc/regression/` (name TBD), write it **as if it were external**
— no `wes_*` imports, its own tests, everything through `ctx` — and add the
"no upward imports" test #034 already calls for. Extracting a clean package is a
day; extracting a tangled one is a month. The interface is the hard part and it
does not require the split.

## Acceptance

- [ ] `Valuer` and `Adjuster` contracts documented in `docs/data-architecture.md`
      alongside the existing 3→4 shape
- [ ] `scale` (`absolute` | `relative`) is part of valuer metadata, and the
      decision layer **refuses** to compare relative values across populations
- [ ] `ctx` is plain data; no strategy performs I/O (enforced by a test, like
      the existing one-HTTP-client rule)
- [ ] `rank_by_points` and `rank_by_zscore` re-expressed as registered valuers
      with **no behaviour change** — pinned by the existing tests
- [ ] Adjusters are bounded and record their contribution in `value_parts`
- [ ] Strategy selected per league in `teams.yaml`, never by the LLM
- [ ] Package has no upward imports (test-enforced)
- [ ] `value: None` means UNKNOWN through every valuer and adjuster — the rule
      that once benched an elite receiver at "0 value" (#029)

## Notes

**2026-08-08 — why this is its own ticket.** #034 owns layer separation and #036
owns the projections themselves; this is the seam both depend on. Sequencing
matters: **#036 should not be built before this exists**, or the Yahoo/ESPN
pipeline gets written once hardcoded and again on the interface.

Existing consumers that come along free once it lands: `optimize_lineup`,
`recommend_roster_moves`, and `wes_draft.best_available` all already consume
`value` and would need no change.

**Open questions.**
1. Where does an ensemble's *disagreement* live? #036 wants source disagreement
   to block auto-execution. That is a decision-layer rail, but the number is
   produced in regression — probably `value_parts` carries per-source values and
   layer 4 applies the threshold. Confirm the seam.
2. Do adjusters compose commutatively? If two each cap at ±15%, does the chain
   cap at ±15% total or compound? **Total**, most likely — decide explicitly and
   test it, or the bound is not a bound.
3. `scale` may be insufficient. Two absolute valuers can still be incomparable
   (Yahoo points vs ESPN points under different scoring). A `unit` alongside
   `scale` may be needed before an ensemble can average them honestly.
4. Package name and location: `pc/regression/` vs promoting the existing
   top-level `fantasy/` (currently only `teams.example.yaml`).

**Status: DESIGN ONLY — not approved to build.** No code written.
