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
