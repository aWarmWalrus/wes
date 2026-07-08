---
name: wes-test
description: Run the WES test suites correctly (unit, e2e, perf, quality eval). Use after any change to Z:\wes code — pc/*, pi/*, or tests/*.
---

# Running the WES test suites

**Prefer the `wes-dev.ps1` helper — it's allowlisted (one rule) so it runs
without a permission prompt, unlike bespoke one-off commands.** It lives PC-side
at `C:\Users\awarm\wes-pc\wes-dev.ps1`; invoke verbs like:

```powershell
& C:\Users\awarm\wes-pc\wes-dev.ps1 test          # unit suite (ignores test_faces.py)
& C:\Users\awarm\wes-pc\wes-dev.ps1 eval          # golden eval, judge=local (add 'haiku' to override)
& C:\Users\awarm\wes-pc\wes-dev.ps1 perf          # perf_check
& C:\Users\awarm\wes-pc\wes-dev.ps1 say voice "what time is it"   # /respond_text, prints reply
& C:\Users\awarm\wes-pc\wes-dev.ps1 reset         # clear all conversation windows
```

(Other verbs: `reload [server|discord|exporters]`, `health`, `turns [n]`,
`usage`, `log [server|discord|exporters] [n]`, `gpu`.) **Do NOT wrap these in
`$env:...;` prefixes or extra pipelines** — that breaks the prefix allowlist
match and forces a prompt (`eval_turns.py` already defaults to `127.0.0.1:8080`,
so no `WES_TEST_URL` is needed). The raw commands below are the equivalents the
helper runs; use them only when you need flags the helper doesn't expose.

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
