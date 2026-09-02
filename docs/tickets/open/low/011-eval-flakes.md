---
id: 011
title: Eval flakes — lexicon-names STT + math-simple arithmetic
status: open
priority: low
created: 2026-07-05
closed:
tags: [eval, stt, testing]
related: [tests/eval/golden.yaml, docs/eval-design.md]
---

## Problem
Two known intermittent eval failures. **Not regressions** — don't panic on a
nightly FAIL that is only these:

- **lexicon-names**: whisper hears "Kaia and Ellis" as "Kaya and Alice"
  (lexicon biasing gap). The 2026-07-05 03:30 nightly FAIL was exactly this.
- **math-simple**: e4b occasionally flubs 12+13 (deterministic `reply_regex`
  check passes, but the local judge scores correctness 0).

## Approach
- Bias/expand the STT lexicon for the names (measured behavior in golden.yaml
  comments).
- Tighten `math-simple`'s `reply_regex`, or accept it as an e4b limitation and
  adjust the case.

## Acceptance
- [ ] lexicon-names passes reliably OR the case is adjusted to reflect reality
- [ ] math-simple no longer flakes the judge

## Notes
Low value; these are cosmetic nightly noise, documented so a FAIL that's only
these doesn't trigger a false alarm.

### 2026-07-29 — this is NOT (only) an STT flake; the case tests the wrong thing
Investigated properly while verifying unrelated work. Pulled the last 12
`lexicon-names` rows out of `eval_history.csv`: **7 FAIL / 4 PASS since
2026-07-21** — a coin flip, not occasional noise. And the diagnosis in this
ticket is wrong:

- **The STT lexicon WORKS.** `transcript_includes: [kaia, ellis]` passes every
  time; only `reply_regex` fails. Whisper is hearing the names correctly now
  (the "Kaya and Alice" mishearing this ticket was opened for appears fixed,
  presumably by the lexicon commit).
- **The real bug is BEHAVIOURAL and it fails 100% of the time.** Asked "Can you
  say hello to Kaia and Ellis for me", the model treats it as a VISION task and
  refuses because it can't see anyone: *"I'm sorry, I don't see anyone in the
  room right now"*, *"my camera is currently unavailable"*. It should simply
  greet them — no camera required.
- **The 4 "passes" are hollow.** They pass only when the reply's TTS→STT
  round-trip happens to echo a name back *inside the refusal* — e.g. "I'm sorry,
  I don't see Kyra or **Ellis** in a room right now" matched `kaia|ellis` and
  scored PASS. So the case currently REWARDS the wrong behaviour whenever
  transcription is kind. That makes it worse than a flake: it's a check that
  can't distinguish success from failure.

Consequence for the queue: this is no longer "cosmetic nightly noise". Either
- fix the routing so a greeting isn't treated as a vision request (the real
  issue, and it likely generalizes to other "do X for/to <person>" phrasings), or
- rewrite the case to assert the *behaviour* (`reply_not_regex` on
  "don't see|can't see|camera") so it stops passing for the wrong reason.

Doing the second alone would turn a 4/12 pass into a hard 0/12 — honest, and it
would stop the case laundering a real bug as a flake. **Also note the nightly's
"REGRESSIONS (passed last run, fail now)" label is misleading for a coin-flip
case** — it compares against only the previous run, so an intermittent case is
reported as a fresh regression roughly half the time.

## Update 2026-09-02 — the STT half is gone entirely

Both named flakes were speech artifacts, and there is no speech any more:

- **`lexicon-names`** was already deleted from the golden set on 2026-08-29 (it
  flipped 0/1/0/1 on identical input, which is worse than no case). The
  contextual-biasing mechanism behind it retired with Whisper on 2026-09-02.
- **`math-simple`** is the only live item left, and it is not an STT flake: the
  deterministic check passes and the local judge occasionally scores correctness
  0 on 12+13. Worth keeping.

Everything below about mishearing, re-transcription and camera-refusal replies
describes a pipeline that no longer exists — read it as history. The
`greeting-brief` observation (the model answering a greeting as if it were a
vision request) may still be worth checking, since the model, not the microphone,
was the cause; but the camera tools it reached for are gone, so it probably
resolved itself.
