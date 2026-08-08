---
id: 036
title: Weekly matchup projections — Yahoo primary, own adjustments on top
status: open
priority: high
created: 2026-08-07
closed:
tags: [fantasy, nfl, data, regression]
related: ["#029", "#034", "#035", docs/data-architecture.md, docs/fantasy-gm-design.md]
---

## Problem / Goal

Every player value in the system today is a **season aggregate**. The engine
cannot tell a great matchup from a terrible one, and it cannot tell a decline in
*volume* from a decline in *touchdown luck*. It is nonetheless making
irreversible drops unattended (#035, `add_drop: auto`), so the cost of that
blindness is now real rather than theoretical.

Concretely, from the first week of full auto:

- **Jordan Addison was auto-dropped 2026-08-01** on 6.25 recent PPG vs 8.15
  season. That is a *points* decline, and points conflate volume with
  efficiency. If his targets held and he simply stopped scoring, the drop was
  wrong and the system had no way to know.
- Every value is a season aggregate, so a player facing the league's best
  defence and one facing its worst are projected identically.

Owner assessment (2026-08-01): these two gaps are *"pretty major to me."*

**Goal**: a weekly per-player projection that is matchup-aware, with an honest
volume signal, without building a full projection system from scratch.

## Approach

**Yahoo's own weekly projection is the primary baseline. Our own signals are
small, bounded adjustments on top.** Owner decision 2026-08-01. This inverts
the previous plan (build our own regression) for three reasons:

1. Yahoo's projection is **denominated in this league's exact scoring**. Our
   ESPN valuation has to re-apply the league's parsed weights itself, which is
   a whole layer (`parse_scoring`) and a standing source of error. Yahoo's
   number already *is* the currency the league scores in.
2. Zero new dependency and zero ToS question — own account, session we already
   hold, page we already load.
3. Matchup and injury adjustment come free, regenerated weekly by Yahoo.

The existing code comment in `wes_yahoo.free_agents` explicitly rejects using
"Yahoo's own displayed projection" in favour of one consistent ESPN scale. That
reasoning was correct **when we only had Yahoo numbers for our own roster**. It
dissolves if Yahoo projects rostered and available players alike — then Yahoo
*is* the single consistent ruler. Confirm before building (see Open Questions).

### Layering

```
projection = yahoo_weekly_proj(player)          # primary, league-scored
           × adjustment(player)                 # ours, BOUNDED (±15% cap)
fallback   = existing ESPN season valuation     # players Yahoo doesn't project
```

Adjustments must be **bounded and confidence-gated**. With a decent baseline an
unbounded correction can essentially only hurt. Store baseline and adjustment
**separately** in the ledger so the Discord DM can say *"Yahoo projects 11.2;
nudged to 12.4 — targets up 40% over three weeks"* — keeping the explanation
honest about which part is a real projection and which is our opinion.

### Adjustment sources, in priority order

**A. ESPN weekly projection as a second opinion — and as a SAFETY RAIL.**
Averaging two independent projections beats either alone. But the larger value
here is disagreement: when Yahoo says 11 and ESPN says 5, that is not a
projection, it is a coin flip. Given unattended irreversible drops are live,
**"the two sources disagree by more than X%" should block auto-execution and
ask the owner instead.** This converts an accuracy feature into a guardrail,
which is worth more than the accuracy.

**B. Volume trend (targets / carries / snap share).** The signal that would have
answered the Addison question. Both Yahoo and ESPN lag role changes by a week or
two; volume moves first. Must be **self-relative** — a player against his own
baseline — which avoids needing team play-count context.

**C. Crowd momentum (Sleeper trending, or Yahoo's own `% Ros` / `% Start`).**
Most useful on the pickup side, which is the weakest part of the system.

**D. Opponent adjustment.** Team-level `byteam` opponent splits are already
fetched — a blunt, non-positional DvP. Only worth it as a small nudge unless
the nflverse layer below makes true defence-vs-position cheap.

**E. Weather, kickers only.** open-meteo is free and keyless; wind above ~15mph
meaningfully hurts field goals. Narrow and seasonal — do last or never.

### Data layer: nflverse, not an ESPN sweep

The obvious way to get volume for the whole league is sweeping ESPN gamelogs.
**Measured 2026-08-01, and it is the wrong answer**: 278 players with ESPN ids,
176.6 KB raw per player, 521 ms each — **48 MB and 278 rate-limited calls per
weekly sweep**, to extract 3 KB per player (98% waste). That is also precisely
the load pattern that produced the cached-empty-payload bug in #035.

nflverse publishes the same data pre-aggregated as GitHub release assets:

| asset | size | verified updated |
|---|---|---|
| `stats_player_regpost_2025.parquet` | 0.25 MB | 2026-07-10 |
| `snap_counts_2025.csv` | 2.29 MB | 2026-02-09 |
| `injuries_2025.csv` | 0.66 MB | 2026-03-18 |
| `ngs_receiving.parquet` | 1.07 MB | 2026-02-28 |

**A full season of weekly player stats is a quarter-megabyte file.** Roughly 4
HTTP requests instead of 278, and the rate-limiting and empty-payload failure
modes disappear entirely.

It also carries what ESPN does not: **snap counts** (a better volume signal than
targets, since it captures role directly), **Next Gen Stats** (air yards,
separation, cushion), official **injury reports**, and **depth charts**.

**Take the `.csv.gz` variants and add no dependency at all** — ~0.14 MB, reads
with stdlib `gzip` + `csv`. Preferred over `nflreadpy` (needs polars + pydantic
+ pandas) for a project this dependency-light with a Pi/PC split. `nfl_data_py`
is effectively superseded and pins `numpy<2.0, pandas<2.0`, which would fight
the existing venv — **do not use it**.

Storage is a non-issue: the parsed subset is under 1 MB for the league's whole
season, and the data is cumulative, so each fetch supersedes the last. Weekly
snapshots are only needed for point-in-time backtesting, and that is ~15 MB for
a season.

Per #034, this is a **raw layer** module feeding the fantasy-data layer; it must
not import upward, and it goes through `wes_http` like everything else.

## Acceptance

- [ ] Yahoo weekly per-player projection scraped for rostered AND available
      players, on one consistent scale
- [ ] ESPN weekly projection fetched as a second opinion, scored under *this
      league's* weights (not ESPN's `appliedTotal`)
- [ ] **Disagreement between the two blocks auto-execution** and routes to the
      owner instead — a guardrail, tested like one
- [ ] nflverse weekly stats + snap counts pulled on a weekly batch task, stored
      PC-local (like the ledger, never the repo), no new dependency
- [ ] `volume_trend` available per player, self-relative, returning `None` for
      UNKNOWN rather than `0.0` (the load-bearing contract from #029)
- [ ] Adjustments bounded (±15%) and confidence-gated; baseline and adjustment
      stored separately in the ledger and surfaced separately in the DM
- [ ] Existing ESPN season valuation retained as fallback for unprojected
      players — nflverse is community-run, so it needs a fallback path
- [ ] Tests: no network, no browser; the guardrail path tested explicitly

## Notes

**2026-08-07 — recon findings, all verified live unless marked otherwise.**

Everything below was probed against the real feeds on 2026-08-01, read-only.

*Yahoo (authenticated session we already have)*
- Team-level projection renders TODAY, in the offseason:
  `class='F-shade proj-pts-matchup'` → `110.39`.
- Players page carries a `Pre-Season` projection column — Jordan Addison
  `114.10` (season-long total).
- Players page also exposes `Tgt*` and `Att*` (season totals — level, not
  trend) and `% Ros` / `% Start`.

*ESPN*
- `lm-api-reads.fantasy.espn.com/.../leaguedefaults/3?view=kona_player_info`
  serves **per-week** projections with no auth: `statSourceId 1` = projected,
  `0` = actual, one entry per `scoringPeriodId`. 2026 already populated.
  Raw projected stat components are in the same payload, so they can be scored
  under this league's weights rather than using ESPN's `appliedTotal`.
- Gamelog columns are **position-specific** (QB gets `passingAttempts`, skill
  players get `receivingTargets`), but `parse_gamelog` zips `names` against
  `stats` and looks each up in a flat dict — so one flat mapping absorbs the
  variation with no per-position branching.
- `receivingTargets` and `rushingAttempts` are present in every gamelog we
  already fetch and are currently discarded. **Do not put them in `cats`** —
  that dict feeds `fantasy_points(cats, scoring)` and means "stats that score".
  Use a sibling `usage` field.
- Scoreboard carries DraftKings lines free (`overUnder`, `spread`), and the
  summary endpoint carries structured injury designations.

*Vegas — mostly ruled out by the owner, recorded for completeness*
- Game-level lines ARE free via ESPN (above) and via Bovada's open JSON
  (200, unauthenticated, Week 1 lines posted). Implied team total is derivable
  with no new source: `overUnder/2 ± spread/2`.
- **DraftKings direct is closed**: 403 on plain HTTP *and* through the real
  Playwright session. Not a parser problem — no 200 at all. Rule it out.
- **Player props could not be verified** — it was the offseason and books had
  not posted them. Bovada showed only Game Lines / Alternate Lines / Score
  Props / Game Props / Correct Score. Re-check in-season before concluding.
- The Odds API (~$30/mo) is the licensed route for props; not worth buying
  until the free path is known to fail.

*Ecosystem survey*
- `nflreadpy` v0.1.5 (2025-11-19) is the current official nflverse Python
  package. `nfl-data-py` v0.3.3 (2024-09-20) is superseded and pins
  `numpy<2.0, pandas<2.0`. `sportsipy` (2021) and
  `pro-football-reference-web-scraper` (2023) are stale; PFR rate-limits hard.
- `espn-api` (2026-03) and `yahoo-fantasy-api` (2026-04) are both maintained;
  the Yahoo one needs the OAuth rejected over the DocuSign/no-caching terms.
- **Sleeper** works: open, documented, no auth. `trending/add` returned real
  counts (85,368 adds/24h for one player). Good crowd signal.
- **ffanalytics has no mature Python equivalent.** Worth stating plainly: it
  aggregates *projections* from many sites; nflverse is *historical stats* and
  is not a substitute. The nearest option is scraping FantasyPros consensus.
  So the ensemble here is two sources, not ten.

**Open questions — MUST be resolved before/while building.**

1. **Does Yahoo render a per-player WEEKLY projection, and on the free-agent
   page too?** Only the season-long `Pre-Season` column is visible in the
   offseason. The whole "Yahoo as single ruler" premise depends on this.
   Unverifiable until Week 1 (2026-09-09).
2. **nflverse in-season update latency.** Every observed timestamp is
   offseason. Documented as nightly with a long track record, but unproven
   here — hence the fallback requirement.
3. **What disagreement threshold should block auto-execution?** Needs real
   in-season data; do not guess it now.
4. **Is Yahoo's projection actually better than our season aggregate?** It has
   a reputation for being conservative and season-average driven. Worth
   measuring rather than assuming — which argues for shipping the scaffold that
   compares the two rulers before depending on either.
5. `MIN_FORM_GAMES` is 3, so volume trend is UNKNOWN through ~Week 3 — exactly
   when rosters churn most. Accept, or find a preseason/depth-chart proxy.

**Sequencing.** Projection ensemble + disagreement rail first (smallest, and it
makes full auto safer immediately). nflverse data layer second (bigger, and it
is what makes everything after it measurable). DvP and the smaller adjustments
only if measurement says they help.

**Status: DESIGN ONLY — not approved to build.** Owner wants to review this in
depth first (2026-08-07). No code written.
