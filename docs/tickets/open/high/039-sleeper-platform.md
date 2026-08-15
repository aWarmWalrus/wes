---
id: 039
title: Sleeper as a second fantasy platform
status: open
priority: high
created: 2026-08-14
closed:
tags: [fantasy, nfl, sleeper, platform]
related: ["#029", "#034", "#035", "#037", docs/fantasy-gm-design.md]
---

## Problem / Goal

The owner has a second NFL league on **Sleeper** — *"Alloy Agents vs. Humans"*,
league `1393935116232818688`, a for-fun agents-versus-humans league — and wants
the same tools and automations to work there, **full-auto**.

Everything in the fantasy stack currently assumes Yahoo: `wes_execute` and
`wes_fantasy` call `wes_yahoo` directly, and `teams.yaml` entries are
Yahoo-shaped.

## Recon — verified live 2026-08-14

**League** (`GET /v1/league/<id>`): 12 teams, 2026, status **`pre_draft`**,
snake draft, 15 rounds, `cpu_autopick` on. Slots
`QB RB RB WR WR TE FLEX FLEX K DEF` + 5 BN. FAAB waivers, $100 budget, trade
deadline week 11. Owner is `awarmwalrus` → **roster_id 3**.

**Sleeper is a far kinder integration than Yahoo**, and the differences are
structural, not cosmetic:

| | Yahoo | Sleeper |
|---|---|---|
| Auth | logged-in browser profile | none |
| Reads | Playwright + DOM scraping | documented JSON API |
| Scoring | text scraped off a settings page, section-aware parser | 43 numeric settings as JSON |
| Player identity | fuzzy name matching | `espn_id` / `gsis_id` / `yahoo_id` in the feed |

**The identity problem is simply absent.** `/v1/players/nfl` is 14MB / 12,218
players in 0.7s and carries `espn_id` (6,736 players), `gsis_id` and `yahoo_id`
— so a Sleeper player joins to our ESPN valuations, and to nflverse (#036), BY
ID. Proven end to end: 121 free agents joined to the ESPN pool by id and ranked
under this league's scoring (McCaffrey 24.51 pts/g, full PPR).

**Note the leagues score differently** — Yahoo is half-PPR, Sleeper is full PPR
(`rec: 1.0`). Per-league scoring was already read per league, so this is
handled, but it means cross-league player values are NOT comparable.

## What is built (2026-08-14)

`pc/wes_sleeper.py`, **read-only**, 33 tests, all network-free:

- `parse_scoring` — Sleeper's 43 settings → our `{weights, tiers, unknown}`,
  the same contract `wes_fantasy.nfl_league_scoring` returns, so the valuer,
  optimizer and executor cannot tell the platforms apart.
- `parse_roster_slots` — `FLEX` → `W/R/T`, `SUPER_FLEX` → `Q/W/R/T`, order
  preserved (Sleeper aligns `starters` POSITIONALLY to this list).
- `parse_players` / `players_index` — the 14MB dump → a slim id index, cached
  6h in-process and fetched with `ttl=0` so the raw body is never pinned in the
  shared HTTP cache.
- `parse_roster` — Sleeper stores no slot on a player; it is implied by index in
  `starters`, and `"0"` means an EMPTY slot rather than a player.
- `free_agents` — Sleeper has no free-agent endpoint; availability is the
  COMPLEMENT of every roster.
- `find_roster_id` — display name → roster id, so registering a league isn't a
  manual id hunt.

The `unknown` field earned its keep on first contact: it surfaced `fgm_60p`
(now mapped to `FG50`; leaving it would undervalue a big-leg kicker) and `ff`
(forced fumbles — kept honestly unknown, since we have no stat to score it).

## THE OPEN QUESTION: writes

**Sleeper's public v1 API is read-only.** There is no documented endpoint for
setting a lineup or making a transaction, so *full-auto is not yet possible* and
`wes_sleeper` deliberately contains no write path — half a write is worse than
none.

Probed (non-mutating): `sleeper.app/graphql` exists and answers unauthenticated
(`{"data":{"__typename":"RootQueryType"}}`). Mutations will require an account
token. Options, none yet chosen:

1. **Internal GraphQL + session token.** Likely how the app itself writes.
   Undocumented, ToS-grey, and it can change without notice — but it would be a
   clean HTTP write path with no browser.
2. **Playwright browser automation**, as with Yahoo. Known-good pattern in this
   codebase, heavier, and the DOM drifts.
3. **Advise-only on Sleeper.** Full read + recommendations, owner executes.
   Loses "full-auto" but costs nothing and is available today.

Worth weighing against the fact that this is explicitly a **for-fun league** —
the same reasoning that made `nfl.l.957011` the auto soak target applies here,
so the risk appetite is high. The constraint is capability, not caution.

## Remaining work

- [ ] **Platform dispatch.** `teams.yaml` gains `platform: yahoo|sleeper`, and
      the engine resolves an adapter instead of importing `wes_yahoo`. The read
      seam is small: `roster_players`, `free_agents`, league scoring/slots,
      `_resolve_team`. The WRITE seam is deeply Yahoo-shaped (`_Session`, URL
      building, DOM) and is what actually needs abstracting.
- [ ] Decide the write path (above), or register Sleeper as advise-only.
- [ ] Register the league in `teams.yaml` — **deliberately not done yet**: the
      league is `pre_draft` and every roster has 0 players, so a GM cycle would
      have nothing to manage and would only add noise.
- [ ] The **draft** is the first real event (snake, 15 rounds, `cpu_autopick`
      on). #030 shelved Yahoo draft automation because the owner drafts
      manually — worth re-asking here, since this is an agents-vs-humans league
      and Sleeper has a documented draft API.
- [ ] Sleeper's own signals we already know are useful: `trending/add` (crowd
      momentum, verified working during #036 recon) and FAAB waiver budgets —
      the latter is the `max_faab_bid_pct` guardrail that is still enforced by
      no code.

## Notes

**2026-08-14 — sequencing.** Reads first because they are unblocked, useful on
their own (recommendations), and independent of however writes get resolved.
Nothing about the read adapter has to change if the write path turns out to be
GraphQL or Playwright.

`pre_draft` also means there is no urgency on the management loop but there IS a
deadline on the draft: `start_time` 1788552000000 (~2026-09-06).
