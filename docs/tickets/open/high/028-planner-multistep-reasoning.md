---
id: 028
title: Planner/orchestrator for ambiguous + multi-step tool queries
status: open
priority: high
created: 2026-07-07
closed:
tags: [router, planner, tools, reasoning, nba, agentic]
related: ["#026", "#027", "#002", docs/pipeline.md]
---

## Problem / Goal
The current loop is a single router pass: e4b (or 12b on deep channels) sees the
tools, usually fires ONE tool, and answers. That breaks on two query shapes the
owner wants (both NBA-flavoured but general):

1. **Ambiguous / missing-tool queries** — "when is the next time the Brooklyn
   Nets play?" There's no single tool that answers it: `nba_scores` is
   today/dated only. The model either guesses (hallucinates a date) or says it
   can't. It needs to recognize the intent maps to "find the team's next
   scheduled game" and use the right data path.
2. **Multi-step / dependent-reasoning queries** — "who are the Brooklyn Nets
   playing right now… who has the most points and rebounds?" This requires a
   CHAIN: (a) find the live Nets game → (b) pull that game's box score → (c)
   reason over the box score to find the max-points and max-rebounds players.
   Step (b) depends on (a)'s output; step (c) is aggregation the tool doesn't do.
   The small router does not reliably plan or carry intermediate results across
   several tool calls, and reasoning over a returned table is exactly what e4b is
   weakest at.

Evidence: `nba_player` already needed a mini-scan to find a player's game; the
"most points/rebounds" ask is that same "reason over the box score" problem one
level up. As we add domains (smart home #004, scheduling #005), the "decompose →
call tools in sequence → synthesize" gap will keep recurring — this is the
general capability, NBA is just the forcing function.

## Approach (to design — this ticket is design-first, don't build yet)
Options, roughly increasing in lift. Pick one (or a staged path) after
prototyping:

- **A. Wider tools, thinner planning.** Add the missing tools so each query is
  one call again: `nba_schedule(team, when="next")`, and a `nba_top_performers`
  that does the box-score aggregation server-side (max pts/reb, leaders). Cheap,
  robust, no new control flow — but doesn't generalize; every new multi-step
  intent needs a new bespoke tool. Good stopgap for the two named NBA queries.
- **B. ReAct-style multi-tool loop.** Let the model iterate: observe tool
  result → decide next tool → … → answer, up to N steps. We already run a
  tool loop; this is "allow >1 round and feed results back." Needs: a step cap,
  a scratchpad of intermediate results in context, and a model strong enough to
  plan (route these to 12b+thinking, per #026). The reasoning-over-results step
  (find the max) lands naturally as the model reading the box score it fetched.
- **C. Plan-and-execute (explicit planner).** A first pass (12b/thinking, or
  even a plan-only prompt) emits a short step list; an executor runs each step's
  tool; a final pass synthesizes. More controllable/inspectable, better for
  logging and for the deferred-action gap (#002), but heavier and slower.

Cross-cutting design questions:
- **Who decides a query is "complex"?** Ties directly to #026 (router allocates
  thinking budget / routes to the deep model). The planner should be the deep
  path; the fast e4b router should DETECT "this needs planning" and hand off,
  not attempt it. So #026's routing decision and this ticket's executor are two
  halves of one system — design them together.
- **Latency vs. correctness.** Multi-round tool loops multiply round-trips
  (each ~2s + inference). Voice has a hard latency budget; Discord tolerates
  more. Likely: full planner on Discord/deep channels, tool-widening (option A)
  as the voice fast-path so common asks stay one call.
- **Grounding / no-hallucination.** Every step's answer must come from a tool
  result carried in context, never the model's memory (same rule as #027). A
  planner that "reasons" must reason over fetched data only.
- **Step cap + failure.** Bounded steps; if a step's tool fails or returns
  nothing, degrade to a partial answer ("they're playing the Celtics; I
  couldn't pull the box score") rather than looping or inventing.

## SotA to mirror (don't reinvent)
- ReAct (reason+act interleaving), Plan-and-Execute / LLMCompiler (separate
  planner + executor), Toolformer/function-calling agents, and the
  "reflexion"/self-critique step cap idea. Anthropic's own tool-use loop guidance
  (multi-turn `tool_use`/`tool_result`) is the reference for option B.
- Reuse before building: if we adopt an MCP client layer (#027 P3), an
  off-the-shelf agent loop may drop in.

## Acceptance (design deliverable)
- [ ] "when is the next time the Nets play?" answers with a real scheduled game
      (date + opponent) from a tool, not a guess
- [ ] "who are the Nets playing right now… who has the most points and
      rebounds?" answers correctly by chaining live-game → box score →
      aggregation, grounded in tool data
- [ ] chosen architecture (A/B/C or staged) written up with the latency budget
      per channel and how it interacts with #026's routing decision
- [ ] step cap + graceful partial-answer behaviour specified
- [ ] no-hallucination guarantee preserved (reason over fetched data only)

## Notes

### 2026-07-21 — Option A (widen tools) shipped for the two named queries
Design decision: **stage this rather than build the full planner now.**
Options B (ReAct loop) and C (explicit plan-and-execute) both depend on #026's
routing decision (who decides a query needs multi-step handling, and at what
thinking budget) — and #026 is itself still open/unresolved (2026-07-21 notes
there: the deep tier can burn its whole budget on `thinking` and yield empty
content, a live bug). Building a multi-round tool loop before that lands would
mean guessing at the budget/step-cap policy twice. Option A has no such
dependency — it's just more tools — so it ships now; B/C stay open pending #026.

**Shipped in `pc/wes_nba.py`** (both closing this ticket's named examples):
- **`next_game(team=None)`** — new ESPN team-schedule lookup (teams list +
  per-team schedule endpoints, distinct from `live_scores`'s today/dated-only
  scoreboard). Answers "when do the Nets next play" with a real opponent +
  date, forward-looking across the season. Tool: `nba_schedule`.
- **`top_performers(team=None)`** — finds the team's live/most-recent game,
  then aggregates the box score server-side (max points, max rebounds across
  both teams) — the "find the game → read the box score → find the max" CHAIN
  from this ticket's example 2, done as ONE tool call/deterministic code
  instead of asking the model to plan multiple rounds. Tool:
  `nba_top_performers`.
- Both registered in `wes_server.TOOLS` + dispatched (16 tools now); both
  degrade to a plain string on any failure (unresolved team, no game, ESPN
  error) — never guess. Unit-tested (fixture-based, no network) in
  `test_unit_nba.py`/`test_unit_server.py`; live canary
  (`WES_NBA_LIVE=1 pytest -k TestLiveESPN`) verified the schedule-endpoint
  schema assumptions against the real API (offseason data: e.g. "The Brooklyn
  Nets next play the Miami Heat on Wednesday, October 14"). Golden cases
  `nba-next-game` / `nba-top-performers` added; full local-judge eval run
  green (only pre-existing `lexicon-names` flake, tracked separately as #011 —
  unrelated to this change).
- **This does NOT close the ticket.** It answers the two named example
  queries, which is exactly what the 2026-07-07 note flagged as the doable
  stopgap; it does not give WES a general decompose-plan-execute capability
  for arbitrary new multi-step intents (smart home #004, scheduling #005 will
  hit the same wall with their own tools). B/C remain open, blocked on #026.

### 2026-07-07: filed at owner request while shipping #027 P1. The two example
  queries are NBA but the capability is general; #026 (adaptive thinking budget)
  is the sibling ticket — the fast router deciding "this needs the planner" is
  #026's job, executing the plan is this ticket's. Consider designing them as
  one system. Option A (a couple of wider NBA tools) is a low-risk stopgap that
  could ship under #027 P1b/P2 independent of the bigger planner decision.
