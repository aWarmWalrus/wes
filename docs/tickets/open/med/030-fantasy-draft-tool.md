---
id: 030
title: Fantasy draft tool — autonomous end-to-end Yahoo draft agent
status: open
priority: med
created: 2026-07-21
closed:
tags: [nba, fantasy, yahoo, draft, tools, agentic, actions]
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
1. **Mock drafts** — Yahoo's mock draft rooms (available year-round, not
   tied to a real league) — the test/soak harness.
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
- [ ] Picks are ranked from real population-wide valuation (z-scores) in the
      league's scoring, not hallucinated and not pure best-player-available —
      accounts for roster construction/positional need
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
