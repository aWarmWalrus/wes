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
all-or-nothing tiers (voice = e4b, escalate to full 12b+thinking on demand;
Discord = full 12b+thinking on EVERY turn — the #001 "sledgehammer"). Cheap
queries should cost little thinking; hard ones get more. Unify the router as the
consistent first step across channels, and make it robust to our weak router.

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

Key adaptation for WES: those papers assume a competent (often trained) router;
ours is gemma e4b, unreliable at tool/routing decisions on text (#001). SotA
offers two flavors — **upfront routing** (classify then allocate; only as good
as the router) vs **cascade/verify-then-escalate** (FrugalGPT; robust to a weak
router because it checks output, not an upfront guess). We should lean on the
cascade's verifier so a weak router can't silently under-serve. Do NOT hand-build
a learned router for a system this size — a prompted/heuristic router + a cheap
verifier is the pragmatic sweet spot.

## Approach
1. **Effort→budget mapping** (L1), mirroring `reasoning_effort`:
   `quick` → 12b, thinking off, ~512 tok · `standard` → 12b, thinking on,
   ~1536 tok · `deep` → 12b, thinking on, ~4096 tok · (trivial → e4b answers,
   no 12b). One mapping table; tunable via env.
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
   e4b triages first on every channel and easy Discord messages get ~1s replies.

## Acceptance
- [ ] `quick`/`standard`/`deep` effort maps to think + num_predict; tunable
- [ ] router allocates effort on escalation; verified it varies by difficulty
- [ ] text-channel verifier re-escalates a narrated/mis-answered turn (covers
      the #001 cases: remember + describe_scene) — verified live
- [ ] router is the first step on all channels; easy Discord turns answer on e4b
- [ ] full eval green; no voice-latency regression (watch the num_ctx/VRAM tie)

## Notes
Supersedes the #001 "route Discord to 12b always" workaround with a principled
version; keep that as the fallback until the verifier is proven. gemma4 does NOT
support graded think levels (tested 2026-07-07: low/med/high produced identical
output), so `effort` must be expressed via think-on/off + `num_predict`, not a
model-native effort level. If we ever want true graded native thinking, that's a
model swap (e.g. gpt-oss) — out of scope here.
