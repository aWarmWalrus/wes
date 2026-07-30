---
id: 030
title: Fantasy draft tool — autonomous end-to-end Yahoo draft agent
status: open
priority: med
created: 2026-07-21
closed:
tags: [nfl, nba, fantasy, yahoo, draft, tools, agentic, actions, multi-sport]
related: ["#029", docs/fantasy-gm-design.md]
---

## Problem / Goal
Build a tool that runs a **full Yahoo fantasy draft autonomously, end to
end** — on the clock, picking for real, no human in the loop. This is
distinct from #029's in-season autonomy model (`advise`/`propose`/`auto` per
team, built around a human owner who wants visibility and veto). The target
use case here is **for-fun leagues where every team is run by an AI agent** —
so the whole premise is unattended, real-time, autonomous drafting, not a
human-approved assist tool.

Must work in two modes against the same code path:
1. **Mock drafts** — Yahoo's mock draft rooms (not tied to a real league) — the
   test/soak harness. **NB (verified 2026-07-22): NOT available in the NBA
   offseason** — they appear preseason (~October); see Notes.
2. **Real drafts** — an actual league's live draft room, same adapter.

## Approach
This reuses the valuation engine but adds a new, harder surface: **live,
time-boxed read+write automation of the draft room itself**, not just the
static roster/scoring scrape #029 P0 already does.

- **New Playwright surface (`pc/wes_yahoo.py` extension or a sibling
  `wes_draft.py`):** the draft room is a different page/app than the
  roster/team pages P0 scraped. Needs: who's on the clock, the live
  draft board (every pick so far, by team), the countdown timer, the user's
  available-player pool, and the pick-submission control. Read AND write,
  scripted clicks per #029 §10's principle — **the LLM/engine chooses the
  player, the script executes the click**, never a model free-driving the
  page.
- **Decision engine — needs the population-wide ranking #029 P1/P2 both
  explicitly deferred:** valuation across the *entire* draftable pool (real
  z-scores, not the interim `roto_scalar` in `pc/wes_fantasy.py`), combined
  with roster construction (positional slots/scarcity — reuse the slot
  eligibility map from `optimize_lineup`, #029 P2) and, ideally, a punt/
  category-balance strategy. Best-player-available alone is a weaker draft
  bot than this data already supports.
- **Snake vs. auction:** confirm which format(s) the target leagues use;
  design the engine mode-agnostic if cheap, but don't build auction bid
  logic speculatively if every target league is snake.
- **Clock pressure is the new failure mode.** Unlike the daily lineup
  (§8.8's "fail-safe = don't act" doesn't apply here — a draft slot with no
  pick is worse than a mediocre one). Needs a **deadline-aware fallback**
  (design §8.8's principle, inverted): if the full decision pipeline can't
  finish before the clock, fall back to a fast best-rank-available pick
  rather than time out and skip a slot. Every pick — fast-path or full —
  gets logged.
- **Resilience:** reconnect/resume if the browser session drops mid-draft;
  re-sync against the live board rather than trusting local state (a missed
  opponent pick desyncs the whole ranking). Draft is a multi-hour, unattended,
  can't-retry-tomorrow event — treat it with the same "nobody is watching"
  rigor as #029 §8.8.
- **Testing path:** Yahoo mock draft rooms are the repeatable test harness —
  run the full agent against real mock-draft rooms (real other-team bots
  picking, real clock) before ever pointing it at a real league's draft. This
  is the draft equivalent of #029 P3's "shadow mode before real writes,"
  except here there's no shadow mode *during* a real draft (it's one-shot) —
  so soak-testing in mocks is the only rehearsal we get.
- **Autonomy model note for docs:** this ticket's autonomy is unconditional
  auto-execute, which is a deliberate departure from #029 §4's per-team
  advise/propose/auto config (that model assumes a human owner; AI-agent-run
  leagues don't have one to propose to). Document this distinction in
  `docs/fantasy-gm-design.md` when this is built, so the two models aren't
  read as inconsistent.

## Acceptance
- [ ] Runs a complete mock draft unattended, start to finish, submitting a
      real pick every turn before the clock expires
- [~] Picks are ranked from real population-wide valuation (z-scores) in the
      league's scoring, not hallucinated and not pure best-player-available —
      accounts for roster construction/positional need. **ENGINE DONE
      2026-07-21** (offline; not yet driving a live draft — see Notes)
- [ ] Survives a simulated disruption (browser hiccup / reconnect) mid-draft
      without missing or double-submitting a pick
- [ ] Deadline fallback verified: forcing the full pipeline to run slow still
      submits a pick before the clock, never a skipped slot
- [ ] Every pick (fast-path or full-decision) is logged (ledger, per #029 §5's
      pattern)
- [ ] At least one full successful run against a real Yahoo mock draft room
      before this is used on any real league
- [ ] Unit tests for the ranking/decision engine (property-tested where
      possible, per [[wes-testing-rigor]]); mock-draft runs stand in for e2e
      since the live room can't be unit-tested directly

## Notes

### 2026-07-23 — NFL is now the FIRST target; dual-sport, sport-specific valuation
Strategy shift (owner): **the owner is joining a real NFL fantasy league this
season** (draft Aug/Sep — sooner than NBA's October), so **NFL is the first live
target**, NBA second. Architecture decision (owner-confirmed):
- **Draft-room automation is sport-AGNOSTIC** — Yahoo's draft client UI is shared
  across sports, so the Playwright ingest (live board, on-the-clock, timer) +
  pick-submit + the draft loop/clock handling are written once, parameterized by
  sport. This is the high-value shared layer, and NFL being live NOW lets us
  recon+build it months before NBA season.
- **Valuation is sport-SPECIFIC behind a common interface.** NBA = roto-category
  z-scores (built, `wes_fantasy.rank_by_zscore`). NFL fantasy is **points-based**
  (PPR/half/standard), not categories, so NFL needs its own valuer (projected
  fantasy points / VORP) + its own player pool (ESPN NFL feed) + position map
  (QB/RB/WR/TE/K/DEF, bye weeks, sharp positional scarcity). Do NOT force-unify
  valuation — share only the `value(player)->number` + `positions` interface and
  the recommender (`best_available`). Matches #029 design's P7 "sport-agnostic
  adapter seams" intent.

**NFL live-draft recon (2026-07-23):** NFL mock/on-demand drafts ARE live now
(unlike NBA). Entry path mapped from the football fantasy home:
- **On-Demand:** `/f1/livedraft_selection` → a **"Draft Now"** button (needs the
  React SPA hydrated before the click registers — wait, don't click immediately)
  → navigates to `/f1/livedraft_waiting?dt=standard`, a **matchmaking/"waiting to
  fill" room** (title "Public League Draft"). Standard on-demand waits to fill
  (maybe for humans); didn't reach a started room in a ~24s autonomous window.
- **Mock lobby:** `/f1/424494/mock_lobby` — scheduled mocks (POST
  `/f1/424494/mock_join`) + an **instant mock** (a `select
  name="instant_mock_position_424494"` of 1st–Nth pick + a `mock_join` form).
  The instant route should fill with bots fast, but the position `select` is a
  custom control my blind `select_option` couldn't operate.
- **Blocker for the live-room DOM:** getting into a *started* room (board / clock
  / pick button) needs the interactive config + fill step — textbook
  **owner-driven recon**. Next: owner runs
  `python pc\yahoo_draft_recon.py https://football.fantasysports.yahoo.com/f1/424494/mock_lobby`,
  starts an instant mock, and captures the started-room screens (board, on-the-
  clock, pick control). Then write the sport-agnostic live-board scraper +
  pick-submit against that DOM.

**NFL engine work (buildable now, in parallel, no Yahoo):** NFL points-based
valuer + ESPN NFL player-pool fetch + NFL position map, behind the same
`best_available` recommender the NBA engine uses.

### 2026-07-22 — live-room recon: session works, but NO offseason mock drafts
Ran a recon session against Yahoo with the persisted Playwright profile. Findings:
- **Session is live** (A1/T/Y auth cookies present) — the #029 P0 login still holds.
- **The "mock drafts available year-round" assumption (below) is WRONG for NBA.**
  Zero "mock" elements on the fantasy home; guessed URLs
  (`/nba/mockdraftlobby`, `/nba/mock`, `/nba/draftcenter`) all 404. Yahoo offers
  no NBA mock drafts in July — real/mock drafts are a preseason thing (~October).
  **So the live-room DOM (on-the-clock, countdown, pick button, available pool)
  can't be captured until preseason.** This is the critical-path blocker for the
  ingest + submit halves; it's a calendar block, not a technical one.
- **But we captured Yahoo's draft-RENDER structure** from the owner's league
  Draft Results page (`/2025/nba/114020/draftresults`), which IS reachable:
  one `<table class="Table Table-interactive …">` per round (header "Round N");
  each pick row = `td.first` (pick #) + `td.player a.name` (the player, with the
  Yahoo id in `href="…/nba/players/<id>"`) + `td.last` (drafting team). The live
  board's picked-list very likely reuses this pattern. NB the id is a **Yahoo**
  player id (Jokić=5352), a different id space from ESPN's (the engine's stats
  source) — a name/id crosswalk will be needed to join a Yahoo-drafted player to
  its ESPN z-score.
- **Built `pc/yahoo_draft_recon.py`** — an owner-drivable capture tool (run on
  the PC like `yahoo_connect.py`): opens the logged-in browser, the owner
  navigates to any draft page, and it dumps that page's DOM + a structured probe
  digest (tables/buttons/clock-candidates/lists/player refs) to timestamped
  files under `wes-pc\draft_recon\`. Validated by capturing the results page.
  **This is the tool that turns the preseason live-room recon into a 5-minute
  owner task** — the moment a mock/real draft room is open, one run captures the
  board + on-the-clock + pick-button DOM so the selectors can be written.

**Next (preseason, when Yahoo enables drafts):** owner opens a mock draft →
`python pc\yahoo_draft_recon.py`, capture the lobby / board / on-the-clock
screens → write the live-board scraper (reusing the round-table pattern above) +
the pick-submit click + the Yahoo↔ESPN player-id crosswalk. Until then the
decision engine (done) is the finished half.

### 2026-07-21 — DECISION ENGINE done (offline); live-room automation still to come
Built the decision half (owner chose "engine first" — it's solo-buildable and
reusable regardless of how the live-room automation lands). What shipped:
- **Real per-league z-scores** in `pc/wes_fantasy.py` — `category_baselines`
  (per-cat mean + stdev over a pool) + `rank_by_zscore`. This is the
  population-based valuation #029 P1/P2 explicitly deferred ("a z-score needs
  the whole league player pool"); it replaces the guessed `_CAT_SPREAD` in
  `roto_scalar` with the pool's actual spread, so value = std-devs above an
  average draftable player and no raw scale (points ~30 vs steals ~1)
  dominates. TO negated; % cats skipped (owner league has none; volume-weighting
  is the refinement). Pure/deterministic.
- **The population fetch, finally solved cheaply.** ESPN's `byathlete` bulk
  endpoint returns hundreds of players' season lines + position in ONE call
  (578 players, paginated) — no per-player fan-out. New `pc/wes_draft.py`:
  `parse_byathlete` (positional value arrays → our stat-line dicts),
  `draftable_pool(limit)` (live fetch, degrades to []).
- **`best_available(ranked, drafted, my_roster, need_weight)`** — the
  recommender: drops everyone taken, applies a positional-need bump (coarse
  G/F/C targets; ESPN only gives coarse position) so a roster thin at a slot
  isn't handed another player it can't use. `recommend_pick` is the end-to-end
  advise entry. All pure except the fetch.
- **Tests:** `test_unit_draft.py` (parser, best_available incl. need-bump,
  recommend_pick degradation) + `TestZScore` in `test_unit_fantasy.py` +
  a `WES_NBA_LIVE=1` canary on the byathlete schema. 332 unit pass.
- **Live-verified** on real 2025-26 data: top z-scores = Jokić, Wembanyama,
  Jalen Johnson, Luka (sane); best_available correctly promoted centers over a
  higher-z guard once 5 guards were rostered (roster construction works).

**What's NOT done (the hard half, needs the owner):** the live Yahoo draft-room
automation — read the live board (on the clock, picks so far, timer, available
pool), submit a pick via scripted Playwright, the deadline fallback, resilience,
and the mock-draft test harness. That's blocked on a recon session against a
real draft-room DOM. **Refinements deferred:** minutes-based / larger pool
(top-scorers pool slightly inflates baselines but not the ranking); real
PG/SG/SF/PF eligibility (ESPN bulk feed is coarse G/F/C only — Yahoo's draft
board would give the fine positions); percentage-category volume-weighting;
snake-vs-auction; wiring an advisory `draft_help` tool into the router. Not yet
committed.

### 2026-07-21 — filed, then rescoped same day
Originally filed as an `advise`-only recommender (pre-draft rankings + "who
should I pick" suggestions for a human drafting manually). **Rescoped per
owner correction:** the actual target is a fully autonomous agent that plays
out an entire draft itself — for AI-agent-run "for-fun" leagues where there's
no human to hand a recommendation to. That makes this a **write/execution**
tool from day one (new territory — #029 P0-P2 are read-only; P3, the first
write, hasn't landed yet), so draft-day automation and #029 P3's lineup
executor will likely want to share the "gated executor" plumbing (§5) even
though this ticket's autonomy is unconditional rather than per-team-moded.
Not on #029's P0→P4 critical path, but on its own hard deadline: a real
draft is a single unrepeatable live event, so this needs to be built and
mock-draft-tested well before the actual draft (NBA season ~October).
