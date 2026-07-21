---
id: 026
title: Adaptive thinking budget — router allocates effort, with a verify-and-escalate net
status: open
priority: med
created: 2026-07-07
closed:
tags: [routing, llm, thinking, latency, architecture]
related: [docs/pipeline.md, "#001", "#002", "#003"]
---

## Goal
Let the router size the *thinking budget* per query instead of the current
all-or-nothing tiers. Cheap queries should cost little thinking; hard ones get
more. Unify the router as the consistent first step across channels.

> **Reframed 2026-07-20 for the single-model topology** (was two models). The
> original premise — a fast weak router (`e4b`) that escalates to a bigger model
> (`12b`) — dissolved when the e4b tag vanished and WES collapsed to **one
> `gemma4:12b`** (2026-07-16). Router and deep tier are now the *same* model, so
> "escalation" already means only *thinking + a bigger token budget on 12b*, not
> a model swap. That makes this ticket **simpler and more central**: the sole
> remaining knob IS the thinking budget, and the current state is binary —
> voice/other = 12b thinking-OFF (plain pass), escalate = 12b thinking-ON;
> Discord = 12b thinking-ON on EVERY turn (the #001 "sledgehammer"). The win is
> to grade that budget by difficulty and stop Discord paying full thinking on
> trivial turns. See the 2026-07-20 note for the full impact.

## Why this is SotA, not a bespoke hack
This is the "adaptive/controllable test-time compute" area — survey **"Reasoning
on a Budget"** (arXiv 2507.02076). Its taxonomy = our two questions:
- **L1 controllability**: a fixed thinking budget knob. Frontier APIs expose
  this as `reasoning_effort` (OpenAI low/med/high), `thinking_budget` tokens
  (Gemini), `budget_tokens` (Anthropic). We should mirror this vocabulary, not
  invent our own.
- **L2 adaptiveness**: the system scales the budget to query difficulty. Named
  problem it fixes: models "overthink simple, underthink hard" problems.

Routing lineage to borrow from (don't rebuild):
- **FrugalGPT** — cascade: router + quality estimator + stop-judge (try cheap,
  verify, escalate).
- **RouteLLM** — router learned from preference data (weak vs strong model).
- **R2-Router** (arXiv 2602.02823, 2026) — closest to this idea: reasons about
  the quality each model hits at different output *lengths*, then picks model +
  a budget constraint. I.e. "router chooses model AND thinking budget."
- **GPT-5 internal router** — the productized version (hidden router → tiers).

Key adaptation for WES: those papers assume a competent (often trained) router.
Ours is now the **12b itself** (was e4b — the collapse upgraded the router; it's
more capable than the e4b that under-called tools on text in #001, though not
frontier-reliable). SotA offers two flavors — **upfront routing** (classify then
allocate; only as good as the router) vs **cascade/verify-then-escalate**
(FrugalGPT; robust to a weak router because it checks output, not an upfront
guess). Keep leaning on the cascade's verifier so the router can't silently
under-serve. Do NOT hand-build a learned router for a system this size — a
prompted/heuristic router + a cheap verifier is the pragmatic sweet spot.

## Approach
1. **Effort→budget mapping** (L1), mirroring `reasoning_effort`, all on the one
   12b: `quick` → thinking off, ~512 tok · `standard` → thinking on, ~1536 tok ·
   `deep` → thinking on, ~4096 tok. One mapping table; tunable via env. (No
   separate trivial tier now that e4b is gone — `quick` IS the floor.)
2. **Router proposes the effort** (L2): add an `effort` arg to the escalate tool
   so the router picks how hard to think when it hands off. Works cleanly on
   voice (tools reliable there).
3. **Verify-and-escalate net** for text channels (the robustness piece): since
   Discord is latency-tolerant, if the router answers a text turn with NO tool
   call / NO escalation but it looks like it needed one (vision/memory/status
   intent, or an action-claim like "I've remembered…"), re-run it escalated.
   This is the FrugalGPT stop-judge, and it's the elegant replacement for #001's
   "always 12b on Discord" — makes the router the first step everywhere without
   trusting its upfront classification. (Overlaps #002's promise-pattern guard.)
4. **Unify**: revert Discord-always-deep (`WES_DEEP_CHANNELS`) once 1-3 hold, so
   the 12b triages `quick` (thinking off) first on every channel and easy Discord
   messages answer fast instead of paying full thinking on every turn.

## Acceptance
- [ ] `quick`/`standard`/`deep` effort maps to think + num_predict; tunable
- [ ] router allocates effort on escalation; verified it varies by difficulty
- [ ] text-channel verifier re-escalates a narrated/mis-answered turn (covers
      the #001 cases: remember + describe_scene) — verified live
- [ ] router is the first step on all channels; easy Discord turns answer `quick`
      (12b, thinking off) instead of full thinking every turn
- [ ] full eval green; no voice-latency regression (watch the num_ctx/VRAM tie)

## Notes

### 2026-07-21 — motivating bug: deep tier thinks past its budget → empty reply
Live evidence for why the budget needs to be adaptive. A hard Discord question (a
Jacobian-conjecture counterexample verification) made the 12b+thinking deep tier
spend its **entire 2048-token budget on `message.thinking`**, emitting **zero
visible content** — `reply: '' (54099ms)` — which the Discord bot showed as
"(no reply)". **Shipped fix (correctness):** `_stream_local` now falls back to
Claude when the deep tier yields no content (`not yielded and deep`), so a hard
problem gets a real answer instead of dead air. **Still open here (latency):** the
turn still burns ~54s thinking before the fallback fires — an *adaptive* budget /
verify-and-escalate would cap the wasted thinking or route "too hard for 12b"
straight to Claude. This bug is the concrete case the ticket should fix.

### 2026-07-20 — refreshed for the single-model topology
The 2026-07-16 collapse to one `gemma4:12b` (docs/pipeline.md, CLAUDE.md) reshapes
this ticket rather than retiring it:
- **The budget IS the only knob left.** With one model, escalation already means
  just thinking-on + a bigger `num_predict`. So "adaptive thinking budget" is no
  longer *one* of several routing levers — it's the whole game. Approach step 2
  (router proposes `effort` via an arg on the escalate tool) is the crux.
- **The `quick`/`standard`/`deep` tiers all run on 12b** — the mapping is
  purely think-flag + `num_predict`, exactly what gemma4 supports (it has NO
  graded native think levels; low/med/high were identical, tested 2026-07-07).
  A model-native effort level would need a model swap (e.g. gpt-oss) — out of
  scope.
- **The verifier (step 3) still earns its keep**, but the pressure is lower: the
  #001 sledgehammer (Discord always 12b+thinking) already fixed the narrated-
  non-tool-call bug, and the router is now the 12b (better than the e4b that
  caused #001), so the verifier is now about *cost* (letting easy Discord turns
  skip thinking) more than *correctness*.
- **Adjacent, not part of this ticket:** the escalation *surface* is growing a
  second target — `search_web` → Claude Haiku with web search for live/current
  info (#029 followup), distinct from `escalate_to_claude` → local 12b+thinking
  for reasoning. When both exist, the router chooses *which handoff* as well as
  the thinking budget; keep the `effort` knob orthogonal to the handoff target.

Supersedes the #001 "route Discord to 12b always" workaround with a principled
version; keep that as the fallback until the verifier is proven.
