# WES tests

Regression tripwires + performance tracking. Everything runs from the PC's
`wes-pc` venv.

> There was a Pi section here — `test_faces.py` ran on the Raspberry Pi under its
> **system** python3, because it needed cv2 and the Hailo runtime. That hardware
> was repurposed on 2026-09-02; the file lives in `archive/pi/` and nothing
> collects it. `archive/pi/README.md` has the full picture.

## CI (GitHub Actions)

`.github/workflows/ci.yml` runs the API-free suite on every push and PR
(ubuntu, Python 3.11 + 3.12) plus a `hosts.yaml` parse check:

```bash
python -m pip install -r requirements-dev.txt
python -m pytest tests/ -q --ignore=tests/test_e2e.py
```

That's the same command locally — `requirements-dev.txt` is the minimal pinned
set to import the modules (no discord.py; see the comments in that file for
why). CI collects `tests/` by directory, so a new `test_unit_*.py` is covered
the day it lands — **keep new unit tests network- and API-free** or they'll fail
there. Only `test_e2e.py` (live server + paid Claude call) is excluded.

## Running them

```powershell
$py = "C:\Users\awarm\wes-pc\.venv\Scripts\python.exe"

# Fast unit tests (no network/API/models) — run these after ANY server change:
& $py -m pytest C:\Users\awarm\wes\tests\test_unit_server.py -q

# End-to-end (real router -> tool loop -> reply; needs the server running):
& $py -m pytest C:\Users\awarm\wes\tests\test_e2e.py --run-e2e -q

# Performance check (records median latency, flags regressions vs history):
& $py C:\Users\awarm\wes\tests\perf_check.py            # 3 runs (default)

# Quality eval (golden set through the live server; flags named-case regressions):
& $py C:\Users\awarm\wes\tests\eval_turns.py            # --only <id> for one case
```

- `test_unit_server.py` — pure logic: `run_tool` dispatch, the tool loop and
  escalation routing, conversation memory and its per-channel policies, the
  turn/usage ledgers, durable memory, the system prompt, and the write
  suppression header.
- `test_e2e.py` — `@e2e` (skipped unless `--run-e2e`): `/health`, a real
  `/respond_text` turn through the `get_datetime` tool, a two-turn
  conversation-memory test (a fact stated in turn one recalled in turn two), and
  a tripwire that the prompt no longer talks as if it were being read aloud.
- `perf_check.py` — posts a fixed question to `/respond_text` N times, records
  the median `llm_ms`/`total_ms` to `perf_history_text.csv`, and exits **1** if a
  metric exceeds `median(recent) * 1.5 + 500ms`.
- `eval_turns.py` — **quality** regressions, phases 1-2 of
  `docs/eval-design.md`: runs each case in `eval/golden.yaml` (multi-turn cases
  via `turns:`, with a `/reset_conversation` before every case) through the live
  `/respond_text`, and applies the case's deterministic checks (`reply_regex`,
  `reply_not_regex` — negative match, e.g. escalation-silent fails any reply
  naming Claude — a `max_reply_chars` brevity ceiling, and a wall-clock bound).
  Each case's `judge:` question is then scored by one LLM-judge call
  (correct/concise/natural 0-2 + hallucination flag) with a selectable
  backend — `--judge haiku` (default; sharper, needs `ANTHROPIC_API_KEY`,
  pennies), `--judge local` (gemma4:12b via Ollama — free/key-less, for nightly
  runs), `--judge both` (agreement check between them), or `--no-judge`. Full
  rationale + calibration notes: `docs/eval-design.md` §3. Appends per-case rows
  to `eval_history.csv`; exits **1** on a named-case deterministic regression
  (passed last run, fails now) or when the judge `correct` average drops >0.3
  below the median of the last 5 **same-backend** runs. Pure logic is
  unit-tested in `test_eval_harness.py`.

### These three used to be audio harnesses

Until 2026-09-02 the eval and the perf check drove `/respond_stream`: each case
was synthesized to a WAV with piper, POSTed as audio, transcribed by the
server's Whisper, and the streamed reply PCM was **re-transcribed** by a local
`tiny.en` model before any check ran. That was genuinely end-to-end, and it
meant a garbled TTS voice failed the eval too.

Both ends of that pipeline are gone. Consequences worth knowing when reading a
history file or an old assertion:

- **The perf metric changed.** It was `ttfa_ms` — request to the first byte of
  reply *audio* — the number a person in the room actually felt. There is no
  first audio byte now, so it is `total_ms`. `perf_history_stream.csv` is kept
  but no longer written; continuing that series would compare a spoken first
  syllable against a finished paragraph.
- **`transcript_includes` was dropped** from the golden set: it asserted that
  STT heard the question, and the question is now sent as text.
- **Brevity moved from seconds to characters, and was re-calibrated, not
  converted** — typed replies are legitimately longer than spoken ones.
- **Some regexes are still loose** for artifacts that can no longer occur
  ("teen" for "team", "bm" for "pm"). They are being tightened case by case as
  each is next touched, so a real regression is never mistaken for a strictness
  change.

## Conventions

- **After changing server logic** → run `test_unit_server.py`.
- **After a change that could affect latency** → run `perf_check.py` and eyeball
  the diff vs baseline.
- **After a change that could affect reply quality** → run `eval_turns.py`.
- **Adding a feature** → add a test for it in the same change.
- `WES_TEST_URL` overrides the server URL (default `http://127.0.0.1:8080`).
- The harnesses send `X-WES-No-Writes: 1`, which suppresses fantasy writes
  server-side for that request. A test must not be able to mutate the owner's
  real Yahoo account — the nightly eval did exactly that at 03:36 every night
  until this existed.
