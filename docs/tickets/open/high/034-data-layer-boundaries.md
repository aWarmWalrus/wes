---
id: 034
title: Enforce data-layer boundaries (raw / data / regression / decision / model)
status: open
priority: high
created: 2026-07-30
closed:
tags: [architecture, refactor, fantasy, nba, nfl, data]
related: [docs/data-architecture.md, "#029", "#027", "#031"]
---

## Problem / Goal

The fantasy/NBA/NFL code grew feature-first to ~5,200 lines across six modules
and the seams blurred. Owner call (2026-07-30): *"let's be smarter about our data
ingestion. we should really make sure we have a clear API boundary between the
raw data layer, the fantasy data layer, the regression layer and the model."*

Audited before designing. What's actually wrong:

- **Three HTTP fetchers** — `wes_nba._get`, `wes_nfl._get_json`,
  `wes_draft._get_json` — three User-Agents, two timeouts (12s / 15s).
- **Caching in exactly one.** `wes_nba` caches with a 20s TTL; the NFL pool's
  **four** ESPN calls and the NBA draft pool are uncached on every invocation.
  This sits inside the ~30s "who should I start" turn, so it is latency and
  politeness-to-ESPN, not tidiness.
- **Two near-identical `byathlete` parsers** (NBA in `wes_draft`, NFL in
  `wes_nfl`) diverging independently.
- **Model-facing formatters scattered through data modules** — `format_roster`,
  `format_scoring`, `format_points`, `format_value`, `format_lineup`,
  `format_board`.
- **`wes_yahoo` does three jobs**: browser scraping, the team registry (identity
  + autonomy policy), and formatting for the model.
- **A sideways import**: `wes_draft` (engine) imports `wes_nba` (raw data).
- **`wes_nfl` spans four layers** (fetch, parse, score, format) — merged
  deliberately for symmetry with `wes_nba`, and a violation under the agreed
  boundary.

**Why it matters, from this week's evidence:** every bug hit recently lived on a
seam. A scraper detail (`span.Fz-xxs`) silently zeroed *valuation*; "no stats"
was indistinguishable from "worth 0" at the data→value boundary; a dead league
key in *config* surfaced as a *model* reply about football. Sharp seams make
those loud instead of silent.

## Approach

Design agreed and written up in **`docs/data-architecture.md`** — layers, the
contracts between them, the rules, and per-seam testing. Summary:

```
5. MODEL       tool entry points, formatting, degradation strings
4. DECISION    optimizer, recommender   (values → recommendations)
3. REGRESSION  valuation / projection   (stats → a number)
2. FANTASY DATA normalization           (source shapes → domain contracts)
1. RAW DATA    transport                (HTTP/DOM, cache, retry, degrade)
        imports point DOWNWARD only
```

Note the **fifth layer**: the agreed framing was raw → data → regression →
model, but `optimize_lineup` *chooses* rather than *estimates*. Folding choice
into regression would put roster rules, guardrails and (later) #029 §5's autonomy
gates inside the layer that must stay pure arithmetic. Decision is therefore kept
separate — surfaced explicitly rather than folded in silently.

**Incremental migration, each step green and shippable alone:**

1. **Shared raw layer** — one HTTP client with caching, retry and one UA.
   Deletes three fetchers; gives the NFL pool caching it never had.
2. **Merge the two `byathlete` parsers** into one sport-parameterized parser.
3. **Split the team registry** out of `wes_yahoo` into its own config module.
4. **Lift the formatters** into the model layer.
5. **Separate regression from decision** in `wes_fantasy`.

Steps 1-2 pay for themselves immediately (real duplication, real latency); 3-5
are structural and can wait.

## Acceptance

- [x] One HTTP client; `wes_nba._get`, `wes_nfl._get_json` and
      `wes_draft._get_json` are gone. **(2026-07-30, `pc/wes_http.py`)**
- [x] Every outbound HTTP fetch is cached by that one client (rate limiting is
      the hook it now has a place for). **(2026-07-30)**
- [ ] One `byathlete` parser serving both sports.
- [ ] No module imports a higher layer; `wes_draft` no longer imports `wes_nba`.
- [ ] Layers 3 and 4 are pure — no network, no clock, no environment.
- [ ] Formatters live in the model layer.
- [ ] The team registry is not inside the Yahoo scraper.
- [ ] Behaviour preserved: the suite stays green and the fantasy golden cases
      keep passing at each step.

## Notes

- **Anti-goals** (also in the design doc): this is not a rewrite; no plugin
  registries or ABCs for two sports — per-sport dict tables (`_SPORTS`, `_SITES`)
  have been the right weight; and NBA/NFL valuation stays separate on purpose
  (roto z-scores vs points are different models sharing only the
  `rank_*(pool) -> [{**player, "value"}]` interface).
- The `value: None` vs `0.0` distinction is now a **contract**, not a
  convention — conflating them benched an elite receiver on 2026-07-29.
- #031 (scraper-drift canary) gets easier after step 1: one client is one place
  to instrument for drift and one place to alert from.

### 2026-07-30 — step 1 DONE: `pc/wes_http.py` is the raw layer

Four hand-rolled fetchers collapsed into one client with per-call TTL caching,
retry-the-transient, and a single User-Agent. Measured, not assumed:

| | before | after |
|---|---|---|
| ESPN pool (4 calls) | 2.33s **every** invocation | 2.33s cold, **0.00s** warm |
| League settings page | **two** Playwright launches per lineup request | one (2.59s → 0.00s) |

The settings finding was a bonus the layering work exposed: `nfl_league_scoring`
cached its parse but `nfl_league_slots` re-fetched **the same page**, so every
lineup request launched a browser twice. Both now go through one cached
`wes_fantasy.league_settings`.

Design decisions worth keeping:
- **Retry the transient, not the doomed.** 429/5xx retry with backoff; 4xx does
  not — a 404 fails identically on retry and only makes the caller wait longer
  for the same bad news.
- **Failures raise at layer 1** and are worded higher up. Layer 1 has no
  vocabulary for talking to a user. A failure is never cached.
- **Per-call TTL, not one global.** "Live score right now" (20s) and "a completed
  season's totals" (900s) do not share a freshness policy.
- **Yahoo stays outside** the client — it's a Playwright session, not HTTP — so
  its caching sits at the composition layer. Documented as a specific exception,
  not a general licence.

A test now asserts no module hand-rolls `urllib.request.urlopen` again, and
another asserts the raw layer imports nothing from higher layers — the
duplication and the direction rule are both enforced rather than just written
down.

One test had to be rewritten rather than kept: `test_rss_fetch_is_cached`
monkeypatched `wes_nba.urllib.request.urlopen` and cleared `wes_nba._text_cache`,
i.e. it reached through the data layer into transport internals. It now injects
at the raw-layer seam and asserts the same user-visible property. That it broke
is the refactor working as intended.

## Layer 3 gains a real interface — see #037 (2026-08-08)

Step 5 ("separate regression from decision") now has a concrete target beyond
separation: **#037 makes layer 3 pluggable**, with registered valuers and
adjusters behind one `value(pool, ctx)` shape. Two consequences for this ticket.

**The 3→4 contract needs a new field.** `{**stat_line, "value": float|None}` is
not sufficient once strategies are swappable, because it does not say whether
the number is comparable across calls. A z-score is relative to the population
it was computed over; points are absolute. `recommend_roster_moves` compares a
rostered player against a free agent, which is only valid for absolute values or
within one population — and it feeds unattended irreversible drops (#035). So
`scale` (`absolute` | `relative`) becomes part of the contract, and layer 4 must
refuse the comparison it cannot make. #037 open question 3 asks whether `unit`
is needed too, since two absolute valuers scored under different rules are still
not averageable.

**The purity rule becomes load-bearing rather than stylistic.** "No network, no
clock, no I/O" in layer 3 is what makes strategies swappable and testable by
arithmetic. #037 keeps it by passing everything through a plain-data `ctx`
assembled by the caller — a Yahoo-projection valuer receives already-scraped
projections, it does not scrape. Worth a test at that seam, in the same spirit
as the one-HTTP-client rule this ticket already enforces.

The "no upward imports" test this ticket calls for is also what lets #037's
package be extracted to its own repo later without untangling anything first.
