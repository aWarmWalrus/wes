# Eval harness design (phases 1-2 BUILT 2026-07-04 — `tests/eval_turns.py`)

Goal: catch **quality** regressions (wrong answers, bad tool routing, misheard
speech, rambling replies) automatically, the way `perf_check.py` already catches
**latency** regressions — no human listening required. Runs on the PC against the
live server; nightly + after meaningful changes.

## Principles

- **Exercise the real pipeline.** Synthesized speech → `/respond_stream` → real
  STT → real LLM (tool loop) → judge the result. No mocks; that's what the unit
  suite is for.
- **Deterministic checks first, LLM-as-judge second.** Cheap rule checks (WER,
  which tools fired, latency, reply length) catch most regressions and never
  flake. One Haiku judge call per turn scores what rules can't (correctness,
  brevity, naturalness). Industry-standard split (ServiceNow EVA, Hamming).
- **History + threshold, like perf_check.** Scores append to a CSV; the run
  fails on a drop vs the recent median. Same mental model, same muscle memory.
- **Production feeds the golden set.** A bad real turn becomes a test case with
  one command.

## Components

### 1. Golden set — `tests/eval/golden.yaml`

~30 cases to start, each:

```yaml
- id: time-basic
  say: "What time is it right now"          # synthesized with piper
  voice: en_US-lessac-medium                # ≠ WES's output voice, stresses STT
  expect:
    transcript_includes: [time]             # STT sanity (or wer_max: 0.3)
    tools: [get_datetime]                   # exact tool routing
    route: local                            # local | claude (escalation check)
    reply_regex: '\d{1,2}[:.]?\d{2}|o.clock' # cheap correctness anchor
  judge: "Does the reply state the current time plausibly and concisely?"
```

Categories: exact-answer (time/date), tool routing (Pi status, look,
describe_scene — vision cases run only when the Pi is reachable), escalation
(multi-step word problem → `route: claude`; "hi" → `route: local`), robustness
(mumbled/short/empty audio → graceful reply, no hallucinated tool output),
conversational (greeting → short, natural, no tool calls).

### 2. Runner — `tests/eval_turns.py`

For each case: synthesize `say` with piper (cached in `tests/eval/fixtures/`,
keyed by text+voice) → `stream_client.post_stream` → collect transcript, reply
text, tool trace, route, timings → run deterministic checks → run the judge →
one row per case.

**Reply text + tool trace need a small server change** (the only one): the
streaming response currently carries only transcript/STT headers. Add to
`/respond_stream`:

- `X-Tools` — JSON list of tool names invoked this turn, in order.
- `X-Route` — `local` | `claude` | `escalated`.
- `X-Reply` — the full reply text (quoted), set via a trailer or — simpler,
  since headers must precede the body — buffered by the eval client from a new
  `?echo_text=1` mode that appends the reply text after the PCM as a
  length-prefixed trailer. (Decide at build time; `X-Tools`/`X-Route` are
  known before streaming starts on the first token, so they're plain headers.
  Fallback: the eval runner can also re-transcribe the reply PCM with the
  server's own Whisper — zero server changes, and it doubles as a TTS
  intelligibility check. Start with re-transcription; add the trailer only if
  WER noise annoys.)

### 3. Judge — one LLM call per case, selectable backend

One grading call per case scores what rules can't: `correct`/`concise`/
`natural` 0-2 + a `hallucination` flag + a one-line `note`. The prompt is
identical for every backend (so backends stay comparable) and carries three
hard-won calibrations — each fixed a real false positive on 2026-07-04:
the **actual date/time** (judge flagged the correct date as hallucinated),
**the assistant's identity** ("runs on a Raspberry Pi" was graded as invented), and
**note-before-scores ordering** (reason first, then grade — cut arithmetic
self-contradictions). It also warns that both sides were round-tripped
through speech recognition, so garbled numbers/years get graded charitably.

Backends (`--judge` flag / `WES_EVAL_JUDGE` env, default `haiku`):

| backend | model | when to use |
|---------|-------|-------------|
| `haiku` | `claude-haiku-4-5` (`WES_EVAL_JUDGE_MODEL`) | deliberate quality work — prompt/model/routing changes. Sharper signal; ~10 calls ≈ pennies. Needs `ANTHROPIC_API_KEY` (without one: prints a hint, judge off). |
| `local` | `gemma4:12b` via Ollama (`WES_EVAL_JUDGE_LOCAL_MODEL`, `WES_OLLAMA_URL`) | nightly/unattended runs — free, key-less, a couple of seconds per case (it's already resident as the VLM; text-only calls skip the slow image prefill). Slightly noisier and more generous. |
| `both` | both | periodic calibration: grades every case with both, prints per-case scores side by side + an agreement summary (mean \|diff\| on `correct`, disagreements by name). Records the haiku scores. Run after changing the judge prompt or the local model, and occasionally before trusting `local` for the nightly gate. |
| `off`  | — | deterministic checks only (`--no-judge` is an alias). |

**Judge ≠ model under test** — self-judging (a model grading its own family's
outputs) is the classic LLM-as-judge bias. Judging is easier than generating,
which is why a 12b can grade replies that Claude wrote.

> ⚠️ **Self-judging since the 2026-07-16 single-model collapse — knowingly
> accepted (2026-07-20).** This rule was written when the router was `gemma4:e4b`
> and the judge `gemma4:12b` (a different, larger model — legitimately
> independent). Now the router **is** `gemma4:12b`, so `--judge local` grades
> replies its own model generated. **Decision: accept the bias** — `--judge
> local` stays the nightly gate (free, key-less), traded off against its
> generosity. Mitigations relied on: the deterministic checks gate independently
> of the judge, `--judge haiku` is used for any decision that matters
> (prompt/routing/model changes), and `--judge both` calibrates periodically. If
> the local median ever drifts implausibly high, revisit (switch nightly to Haiku
> or pull a separate small judge model).

Each history row records its `judge` backend, and the score gate (§4) only
compares runs judged by the **same** backend — haiku and local have different
baselines (local runs ~generous), so mixing their medians would make the gate
meaningless. Rows from before the column existed count as haiku.

First calibration run (2026-07-04, `--judge both`, 8 judged cases): mean
\|diff\| 0.25 on `correct`, 7/8 identical; the one disagreement was a
re-transcription artifact ("2:11 PM" garbled to "to 11 p.m."), where local's
0 was arguably the honest grade. Local judge output that fails JSON parsing
is reported and the case left unscored (happened once — it never breaks a
run).

### 4. Scoring + gate — `tests/eval_history.csv`

As built: one row **per case** per run (ts, pass/fail + failure reasons,
transcript, re-transcribed reply, timings, judge scores + note) — run-level
aggregates are computed from the rows, and richer per-case history beats
pre-aggregated rows for debugging. Exit 1 if any deterministic check that
passed in the last recorded run now fails (a *named-case* regression list,
printed), or the judge `correct` average drops >0.3 vs the median of the last
5 runs. Named-case tracking matters more than the aggregate: "time-basic
broke" is actionable; "score dipped 4%" is not. (Judge noise exists — tiny.en
re-transcription garbles spoken years, which can cost a case 2 points on a
given run — hence gating on the average with tolerance, never per-case judge
scores.)

### 5. Production → golden set

Extend the Pi's `timing.csv` row (or a new `turns.jsonl` on the PC, one JSON
object per turn: transcript, reply, tools, route, timings, turn id). Then
`eval_turns.py --promote <turn-id>` copies a real turn into `golden.yaml` as a
skeleton case (re-synthesizing the transcript as the `say`). Bad real turns are
the highest-value regression cases.

### 6. Escalation audit — `eval_turns.py --audit-routing [N]`

Offline, no audio: replay the last N logged transcripts through *both*
backends (`_think_local` w/o escalation vs `_think_claude`), judge both
replies, and report where gemma answered worse than Claude but didn't escalate
(under-escalation → wrong answers spoken) and where it escalated for a query
it matched Claude on (over-escalation → cost/latency). Output: a rate, plus
the offending transcripts. Run weekly, not nightly.

### 7. Scheduling — BUILT (2026-07-04)

The "WES Nightly Eval" scheduled task (daily 3:30 AM, registered like "WES
Server") runs `C:\Users\awarm\wes-pc\run_nightly_eval.ps1`: `perf_check.py`
+ `eval_turns.py --judge local` (free, key-less — see §3; the noisier local
judge is fine for drift-catching) against the live server, appending to both
histories. One verdict line per night goes to
`C:\Users\awarm\wes-pc\logs\eval.log` (`<stamp>  perf OK  eval OK`); the full
output of the most recent run is in `logs\eval_last.log` (UTF-16, like
server.log). Red = look; green = ignore. Log-only by design: WES never
speaks on its own initiative (house rule — no audio without confirmation).
Verified by triggering the task manually: exit 0, verdict line written,
eval 10/10 under the local judge.

## Build order

1. ✅ `golden.yaml` (10 cases) + runner with deterministic checks only, reply
   text via re-transcription. **This alone catches routing + STT regressions.**
   (Proved out on its first run: caught gemma refusing "capital of France";
   fixed via a system-prompt line. Baseline 9/9, pi-status skips when the Pi
   is down.)
2. ✅ Judge + history gate (2026-07-04). One judge call per case with
   selectable backends — `haiku` (default), `local` (gemma4:12b), `both`
   (agreement check), `off`; see §3. Judge prompt carries the actual
   date/time and the assistant's identity (without them it flagged the correct date
   and "runs on a Raspberry Pi" as hallucinations), and asks for the `note`
   reasoning *before* the scores. Gate: exit 1 if the run's `correct` average
   drops >0.3 below the median of the last 5 same-backend runs. Proved out
   immediately: caught gemma restating "12 plus 13" as "fifteen plus twelve",
   and reading the raw tool clock aloud ("one three twenty colon forty six")
   — fixed by making `get_datetime` return spoken-friendly text.
3. `X-Tools`/`X-Route` headers (small `wes_server.py` change + unit test) to
   make routing assertions exact instead of inferred.
4. `turns.jsonl` + `--promote`.
5. `--audit-routing`; ✅ nightly scheduled task (2026-07-04, see §7).

Storage budget: negligible by design — fixtures are short piper WAVs
(~100-300KB each, ~10MB for 30 cases), histories are CSVs, and `turns.jsonl`
is text-only (no audio retention; cap it at ~5MB with rotation). C: headroom
is tight, so the harness must never cache reply audio to disk.

Non-goals for now: audio-quality scoring of the TTS waveform (re-transcription
WER is the proxy), simulated acoustic noise (add noise-mixed fixtures later if
real-world STT misses show up in `turns.jsonl`).

Multi-turn cases ARE supported (2026-07-04, alongside conversation memory):
give a case `turns:` (a list of utterances) instead of `say:` — they post in
order as one conversation and all checks/judging apply to the last turn's
reply. The runner POSTs `/reset_conversation` before every case so cases stay
order-independent now that the server is stateful. See `memory-recall` in
`golden.yaml`.
