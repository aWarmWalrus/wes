# Data architecture — layer boundaries

> Status: **design agreed 2026-07-30, migration in progress** (ticket #034).
> This is the target shape and the rules that keep it. Where today's code differs,
> the "Current → target" table says so; nothing here is aspirational hand-waving.

## Why

The fantasy/NBA/NFL code grew feature-first across ~5,200 lines in six modules,
and the seams blurred. Concretely, before this doc existed:

- **Three HTTP fetchers** — `wes_nba._get`, `wes_nfl._get_json`,
  `wes_draft._get_json` — with three User-Agent strings and two timeouts.
- **Caching in exactly one of them.** `wes_nba` has a 20s TTL cache; the NFL
  pool's *four* ESPN calls and the NBA draft pool were uncached on every
  invocation. That is a real cost, not a tidiness complaint: it sits inside the
  ~30s "who should I start" turn.
- **Two near-identical `byathlete` parsers** (NBA in `wes_draft`, NFL in
  `wes_nfl`) — same endpoint, same zip-the-labels logic, diverging separately.
- **Model-facing formatters scattered through data modules** —
  `format_roster`/`format_scoring` in the scraper, `format_points` in the NFL
  engine, `format_value`/`format_lineup` in the fantasy engine, `format_board`
  in the draft engine.
- **`wes_yahoo` doing three jobs**: browser scraping, the team registry
  (identity + autonomy policy), and formatting for the model.
- **A layer skip**: `wes_draft` (engine) importing `wes_nba` (raw data) directly.

The cost of blur is not aesthetic. Every bug this project hit in the last week
lived exactly on a seam: a scraper detail (`span.Fz-xxs`) silently zeroing
*valuation*; "no stats" being indistinguishable from "worth 0" at the
data→value boundary; a dead league key in *config* surfacing as a *model* reply
about football. Sharp seams are where those become loud instead of silent.

## The layers

Four layers, plus one the four-layer framing hides — called out rather than
folded in silently, because it's a real design question (see "The fifth
concern").

```
  ┌─────────────────────────────────────────────────────────┐
  │ 5. MODEL          tool entry points, formatting, degradation strings
  ├─────────────────────────────────────────────────────────┤
  │ 4. DECISION       optimizer, recommender  (consumes values → recommendations)
  ├─────────────────────────────────────────────────────────┤
  │ 3. REGRESSION     valuation/projection    (stats → a number)
  ├─────────────────────────────────────────────────────────┤
  │ 2. FANTASY DATA   normalization           (source shapes → domain contracts)
  ├─────────────────────────────────────────────────────────┤
  │ 1. RAW DATA       transport               (HTTP/DOM, cache, retry, degrade)
  └─────────────────────────────────────────────────────────┘
        imports point DOWNWARD only. Never sideways, never up.
```

### 1. Raw data — transport

**Knows:** HTTP, browser sessions, caching, timeouts, retries, User-Agents,
which URL to hit.
**Must not know:** what a fantasy point is, what a roster is, which sport.

Returns bytes / parsed JSON / a DOM page handle. Its only domain concept is
"this fetch failed", which it reports as a degradation, never an exception.

*One fetcher, one cache, one retry policy.* Rate limiting and politeness to
ESPN/Yahoo are enforced here or nowhere.

**One documented exception:** Yahoo is reached through a persistent Playwright
browser session, not HTTP, so it cannot sit behind this client. Its caching lives
at the composition layer (`wes_fantasy.league_settings`). Any *new* HTTP source
belongs here — the exception is Yahoo's browser session specifically, not a
general licence.

### 2. Fantasy data — normalization

**Knows:** ESPN's stat labels, Yahoo's DOM, sport-specific spellings, and the
domain contracts below.
**Must not know:** how anything is *valued*, or how a reply is worded.

This is where sport asymmetry is absorbed so nothing above it branches on sport
for parsing reasons. It is also where source-specific traps are handled and
documented — `passing.sacks` being sacks *taken*, `/f1/` not `/nfl/`,
`.ysf-player-detail` meaning different things per sport.

### 3. Regression — valuation / projection

**Knows:** how to turn a normalized stat line into a number, given a league's
scoring: z-scores against a pool, points under weights, per-game normalization,
and later regression-to-mean and projections.
**Must not know:** HTTP, Yahoo, DOM, or the LLM.

**Pure and deterministic.** No network, no clock, no I/O. This is the layer
whose correctness is checkable by arithmetic, so it must stay trivially testable.

### 4. Decision — optimizer / recommender

**Knows:** values, roster slots, eligibility, guardrails.
**Must not know:** where the values came from.

`optimize_lineup` and `best_available` live here. Also pure.

### 5. Model — everything the LLM touches

**Knows:** tool schemas, how to phrase a result, what caveats must survive.
**Must not know:** how any number was computed.

Every `format_*` belongs here, along with the tool entry points and the rule
that entry points degrade to a string and never raise into a turn. **Caveats are
load-bearing at this layer**: if the decision layer says values are missing, the
model layer must say so (`unknown_value` → the WARNING line).

## The fifth concern

The agreed framing was *raw → fantasy data → regression → model*. But
`optimize_lineup` doesn't estimate a value; it **chooses** given values. Folding
choice into "regression" would mean the layer that must stay pure arithmetic also
carries roster rules, guardrails and, later, the autonomy gates from #029 §5 —
which is exactly the mixture that makes a layer untestable.

So decision is kept separate above regression. It costs one extra module and
buys the property that the valuation layer never grows policy.

## Contracts between layers

These shapes are the API. Changing one is a cross-layer change.

```python
# 2 → 3   a normalized stat line
{"name": str, "season": str, "gp": float|None, "team": str,
 "positions": [str], "cats": {stat_key: number}}

# 2 → 4   a roster slot occupant
{"name": str, "team": str, "positions": [str], "slot": str,
 "status": str, "game": str, "player_key": str}

# 2 → 3   a league's scoring
{"scoring_type": str, "categories": [str]}                     # category leagues
{"weights": {key: float}, "tiers": [(int, float)], "unknown": [str]}   # points

# 3 → 4   a valued player  (value is None when UNKNOWN, never 0-as-unknown)
{**stat_line, "value": float|None}

# 4 → 5   a recommendation
{"starters": [{"slot","name","value"}], "bench": [str],
 "empty_slots": [str], "total": float, "sport": str,
 "unknown_value": [str]}
```

**`value: None` means unknown and `0.0` means zero.** They are different facts
and the contract keeps them different — conflating them silently benched an
elite receiver on 2026-07-29.

## Current → target

| Today | Layer(s) it occupies now | Target |
|---|---|---|
| `wes_nba.py` | 1 + 2 + 5 (fetch, parse, format) | split: transport → raw client; parsing → nba data; formatters → model |
| `wes_nfl.py` | 1 + 2 + 3 + 5 | split: pool fetch → raw; `parse_*` → data; scoring → regression; `format_points` → model |
| `wes_yahoo.py` | 1 + 2 + 5 + config | split: session/scrape → raw; extractors → data; formatters → model; **team registry → its own config module** |
| `wes_fantasy.py` | 3 + 4 + 5 (+ composition) | `roto_scalar`/`rank_by_zscore` → regression; `optimize_lineup` → decision; `format_*`/tool entries → model |
| `wes_draft.py` | 1 + 2 + 3 + 4 | pool → raw/data; `best_available` → decision |
| `wes_server.py` | 5 | unchanged — it is the model layer |

## Rules

1. **Imports point downward only.** A lower layer must never import a higher one.
   `wes_draft` importing `wes_nba` was a sideways import and is exactly the
   pattern to kill.
2. **Layer 3 and 4 are pure.** No network, no clock, no environment. If a
   function needs "now", it takes it as an argument.
3. **Failures degrade, never raise** — chosen at layer 1, preserved upward, and
   turned into a sentence at layer 5.
4. **Injectable seams.** Every cross-layer call takes an optional `_fn` override
   so the layer above is testable without the layer below (already the house
   style: `_get_fn`, `_players_fn`, `_valmap_fn`).
5. **Traps get documented where they're absorbed**, at layer 2, next to the code
   that handles them — not in the caller.
6. **A caveat that reaches layer 5 must reach the user.** Dropping
   `unknown_value` is a bug, not a formatting choice.

## Testing each seam

The point of sharp seams is that each layer is testable alone, from fixtures:

| Layer | Tested with | Fixture |
|---|---|---|
| 1 raw | injected `_get_fn`; no live calls in unit tests | — |
| 2 data | real captured payloads | `tests/fixtures/espn_nfl_byathlete.json`, `yahoo_nfl_settings.txt` |
| 3 regression | hand-built stat lines; arithmetic assertions | inline |
| 4 decision | hand-built valued players; brute-force property test | inline |
| 5 model | injected tool functions; golden eval for phrasing | `tests/eval/golden.yaml` |

A test that needs three layers to exercise one behaviour is a signal the seam is
in the wrong place. (`test_unit_nfl.py` importing `wes_fantasy` for the
interface-parity check is *deliberate* — it pins the 3→4 contract — but it
should be the exception, and named as such.)

## Migration

Incremental, each step green and shippable on its own:

1. **Shared raw layer** — one HTTP client with caching, retry and one UA.
   Deletes three fetchers, gives the NFL pool the caching it never had.
   **DONE 2026-07-30** (`pc/wes_http.py`). Measured: the ESPN pool's four calls
   go 2.33s cold → 0.00s warm, and the league-settings page went from TWO
   Playwright launches per lineup request to one (2.59s → 0.00s), because
   scoring and slots read the same page and each fetched it separately.
2. **Merge the two `byathlete` parsers** into one sport-parameterized parser.
   *Next.*
3. **Split the team registry** out of `wes_yahoo` into its own config module.
4. **Lift the formatters** into the model layer.
5. **Separate regression from decision** in `wes_fantasy`.

Steps 1-2 pay for themselves immediately. 3-5 are structural and can wait for a
quiet moment.

## Anti-goals

- **Not a rewrite.** Every step preserves behaviour; the test suite is the proof.
- **No abstraction for its own sake.** No plugin registries or ABCs for two
  sports. A dict of per-sport tables (`_SPORTS`, `_SITES`) has been the right
  weight so far — keep that pattern.
- **Don't unify NBA and NFL valuation.** Roto z-scores and points-based scoring
  are genuinely different models. They share the *interface*
  (`rank_*(pool) -> [{**player, "value"}]`), not the mathematics.
