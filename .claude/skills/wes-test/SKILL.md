---
name: wes-test
description: Run the WES test suites correctly (unit, e2e, perf, quality eval). Use after any change to Z:\wes code — pc/*, pi/*, or tests/*.
---

# Running the WES test suites

All PC-side testing uses the Windows venv interpreter (never system python,
never a Pi path):

```powershell
$py = "C:\Users\awarm\wes-pc\.venv\Scripts\python.exe"
```

## Unit tests (every change)

```powershell
cd Z:\wes\tests
& $py -m pytest -q --ignore=test_faces.py
```

- `--ignore=test_faces.py` is REQUIRED on the PC: that file imports Pi-only
  deps (cv2/hailo) and aborts collection. It runs on the Pi instead:
  `ssh walrus-pi "python3 ~/claude/wes/tests/test_faces.py"` (system python3,
  not the venv).
- House rule: add a test in the same change when adding a feature.

## Latency (any change that could touch the hot path)

```powershell
& $py Z:\wes\tests\perf_check.py     # needs the live server on :8080
```

Flags regressions vs the rolling baseline; appends to
`tests/perf_history_stream.csv`.

## Reply quality (prompts, routing, models, TTS changes)

```powershell
& $py Z:\wes\tests\eval_turns.py --judge local   # free 12b judge; needs live server
```

- `--judge haiku` costs Claude calls; `--judge both` measures judge agreement;
  `--no-judge` for deterministic checks only.
- Known flake: `lexicon-names` sometimes fails on an STT mishearing ("Kaya and
  Alice" for "Kaia and Ellis"). One rerun distinguishes flake from regression.
- The runner POSTs `/reset_conversation` (empty body = clears ALL channels)
  before each case.

## E2E (full pipeline)

```powershell
& $py -m pytest Z:\wes\tests\test_e2e.py --run-e2e -q   # needs live server
```

## Endpoint smoke tests without audio

`POST /respond_text` is text-in/text-out (no STT/TTS) — the cheapest way to
exercise the LLM path end-to-end:

```powershell
Invoke-RestMethod -Uri http://127.0.0.1:8080/respond_text -Method Post `
  -ContentType 'application/json' -Body '{"text":"...","channel":"discord"}'
# then clean up so the test turn doesn't linger in conversation memory:
Invoke-RestMethod -Uri http://127.0.0.1:8080/reset_conversation -Method Post `
  -ContentType 'application/json' -Body '{"channel":"discord"}'
```

After changing `pc/wes_server.py`, the live server must be reloaded before
perf/eval/e2e reflect the change — see the `wes-reload` skill.
