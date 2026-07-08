# WES tests

Regression tripwires + performance tracking. The system spans two hosts, so the
tests do too — run each host's set with that host's interpreter.

## PC (server logic + e2e + perf) — from the `wes-pc` venv

```powershell
$py = "C:\Users\awarm\wes-pc\.venv\Scripts\python.exe"

# Fast unit tests (no network/API/models) — run these after ANY server change:
& $py -m pytest Z:\wes\tests\test_unit_server.py -q

# End-to-end (real STT -> LLM -> streaming TTS; needs the server running):
& $py -m pytest Z:\wes\tests\test_e2e.py --run-e2e -q

# Performance check (records median latency, flags regressions vs history):
& $py Z:\wes\tests\perf_check.py            # 3 runs (default)

# Quality eval (golden set through the live server; flags named-case regressions):
& $py Z:\wes\tests\eval_turns.py            # --only <id> for one case
```

- `test_unit_server.py` — pure logic: `_vlm_prompt` (identity/clothing prompt),
  `next_sentence` (TTS sentence chunking), `_norm` + `lookup_speculation` (spec
  cache prefix match), rate-limit budget, `run_tool` dispatch, scene-cache TTL.
- `test_e2e.py` — `@e2e` (skipped unless `--run-e2e`): `/health`, then a piper→STT
  round-trip through **`/respond_stream` (the production path the Pi uses)** —
  transcript recovered, streamed PCM reply, timing headers intact — plus a
  two-turn conversation-memory test (fact stated in turn one recalled in turn
  two) and one test keeping the legacy blocking `/respond` fallback honest.
- `perf_check.py` — posts a fixed synthesized WAV to **`/respond_stream`** N
  times, records the median `stt/ttfa/total` ms to `perf_history_stream.csv`, and
  exits **1** if a metric exceeds `median(recent) * 1.5 + 500ms`. `ttfa_ms`
  (first reply audio byte) is the perceived-latency metric. `perf_history.csv`
  is the retired history of the old `/respond` path (different schema — kept
  for reference, no longer written).
- `stream_client.py` — shared `/respond_stream` client (drains the PCM stream,
  measures headers/ttfa/total) used by both of the above.
- `eval_turns.py` — **quality** regressions, phases 1-2 of
  `docs/eval-design.md`: runs each case in `eval/golden.yaml` (fixtures
  synthesized/cached in `eval/fixtures/`; multi-turn cases via `turns:`, with
  a `/reset_conversation` before every case) through the live `/respond_stream`,
  **re-transcribes the reply PCM** with a local tiny.en whisper (so garbled
  TTS fails too), and applies the case's deterministic checks
  (`transcript_includes`, `reply_regex`, `reply_not_regex` — negative match,
  e.g. escalation-silent fails any reply naming Claude — audio-length brevity
  bounds, wall-clock bound). Each case's `judge:` question is then scored by one
  LLM-judge call (correct/concise/natural 0-2 + hallucination flag) with a
  selectable backend — `--judge haiku` (default; sharper, needs
  `ANTHROPIC_API_KEY`, pennies), `--judge local` (gemma4:12b via Ollama —
  free/key-less, for nightly runs; never e4b, the model under test),
  `--judge both` (agreement check between them), or `--no-judge`. Full
  rationale + calibration notes: `docs/eval-design.md` §3. Appends per-case
  rows to `eval_history.csv`; exits **1** on a named-case deterministic
  regression (passed last run, fails now) or when the judge `correct`
  average drops >0.3 below the median of the last 5 **same-backend** runs.
  Pi-dependent cases skip when `:8090` is down. Real bugs caught on first
  runs: gemma refusing "capital of France" (phase 1), gemma misreading the
  raw clock string aloud and restating "12 plus 13" as "fifteen plus twelve"
  (phase 2). Pure logic is unit-tested in `test_eval_harness.py`.

## Pi (vision / face pipeline) — SYSTEM python3 (needs cv2/hailo)

```bash
python3 ~/claude/wes/tests/test_faces.py
```

Covers the numpy/cv2 helpers: `match` (recognition), `_position`,
`_clothing_color`, `_distance2bbox`, `_nms`, and gallery I/O. No Hailo/camera
needed — the model inference paths are exercised by the ad-hoc `hailo_*` scripts.

## CI (GitHub Actions)

`.github/workflows/ci.yml` runs the **fast unit suite** (`test_unit_server.py`,
`test_unit_discord.py`, `test_unit_hosts.py`, `test_eval_harness.py`) on every
push and PR, across Python 3.11/3.12. It installs only `requirements-dev.txt`
(pytest + the light server deps — flask/anthropic/prometheus-client/pyyaml); the
STT/TTS stack is imported lazily by the server so CI never needs it. The e2e,
perf, and eval tiers stay out of CI — they need a live server, models, or Pi
hardware — so keep unit tests hardware/API-free to preserve this.

## Conventions

- **After changing server logic** → run `test_unit_server.py`.
- **After changing `hailo_faces.py`** → run `test_faces.py` on the Pi.
- **After a change that could affect latency** → run `perf_check.py` and eyeball
  the diff vs baseline.
- **Adding a feature** → add a test for it in the same change.
- `WES_TEST_URL` overrides the server URL (default `http://127.0.0.1:8080`).
