---
name: wes-test
description: Run the WES test suites correctly (unit, e2e, perf, quality eval). Use after any change to C:\Users\awarm\wes code — pc/* or tests/*.
---

# Running the WES test suites

> **There is one clone: `C:\Users\awarm\wes`.** A second one lived on the
> Raspberry Pi and was reachable as `Z:\wes`; pointing pytest at it tested a
> copy that could be on a different commit than the running code — the tests
> passed and proved nothing. The Pi was repurposed on 2026-09-02 and `Z:` goes
> with it, so this survives only as a warning about stale notes.

**Prefer the `wes-dev.ps1` helper — it's allowlisted (one rule) so it runs
without a permission prompt, unlike bespoke one-off commands.** It lives PC-side
at `C:\Users\awarm\wes-pc\wes-dev.ps1`; invoke verbs like:

```powershell
& C:\Users\awarm\wes-pc\wes-dev.ps1 test          # unit suite
& C:\Users\awarm\wes-pc\wes-dev.ps1 eval          # golden eval, judge=local (add 'haiku' to override)
& C:\Users\awarm\wes-pc\wes-dev.ps1 perf          # perf_check
& C:\Users\awarm\wes-pc\wes-dev.ps1 say probe "what time is it"   # /respond_text, prints reply
& C:\Users\awarm\wes-pc\wes-dev.ps1 reset         # clear all conversation windows
```

(Other verbs: `reload [server|discord|exporters]`, `health`, `turns [n]`,
`usage`, `log [server|discord|exporters] [n]`, `gpu`, and `models
[status|check|list|load|unload|fit]` — the VRAM/model manager: pin models in
VRAM, free them, or drift-check config vs reality.) **Do NOT wrap these in
`$env:...;` prefixes or extra pipelines** — that breaks the prefix allowlist
match and forces a prompt (`eval_turns.py` already defaults to `127.0.0.1:8080`,
so no `WES_TEST_URL` is needed). The raw commands below are the equivalents the
helper runs; use them only when you need flags the helper doesn't expose.

All testing uses the Windows venv interpreter, never system python:

```powershell
$py = "C:\Users\awarm\wes-pc\.venv\Scripts\python.exe"
```

## Unit tests (every change)

```powershell
cd C:\Users\awarm\wes\tests
& $py -m pytest -q
```

- `--ignore=test_faces.py` used to be REQUIRED here: that file imported Pi-only
  deps (cv2/hailo) and aborted collection, and it ran on the Pi under system
  python3. It moved to `archive/pi/` with the rest of that tier (2026-09-02) and
  nothing collects it, so the flag is gone. `test_e2e.py` is still excluded from
  a plain run — it needs a live server and a paid Claude call.
- House rule: add a test in the same change when adding a feature.

## Latency (any change that could touch the hot path)

```powershell
& $py C:\Users\awarm\wes\tests\perf_check.py     # needs the live server on :8080
```

Flags regressions vs the rolling baseline; appends to
`tests/perf_history_text.csv`. (`perf_history_stream.csv` is the frozen history
of the retired voice path — different metrics, no longer written.)

## Reply quality (prompts, routing, models)

```powershell
& $py C:\Users\awarm\wes\tests\eval_turns.py --judge local   # free 12b judge; needs live server
```

- `--judge haiku` costs Claude calls; `--judge both` measures judge agreement;
  `--no-judge` for deterministic checks only.
- Known flake: `lexicon-names` sometimes fails on an STT mishearing ("Kaya and
  Alice" for "Kaia and Ellis"). One rerun distinguishes flake from regression.
- The runner POSTs `/reset_conversation` (empty body = clears ALL channels)
  before each case.

## E2E (full pipeline)

```powershell
& $py -m pytest C:\Users\awarm\wes\tests\test_e2e.py --run-e2e -q   # needs live server
```

## Endpoint smoke tests

`POST /respond_text` is the whole interactive surface — it was the text-only
alternative to the audio endpoints, which retired with the Pi — and it is the
cheapest way to exercise the LLM path end-to-end:

```powershell
Invoke-RestMethod -Uri http://127.0.0.1:8080/respond_text -Method Post `
  -ContentType 'application/json' -Body '{"text":"...","channel":"discord"}'
# then clean up so the test turn doesn't linger in conversation memory:
Invoke-RestMethod -Uri http://127.0.0.1:8080/reset_conversation -Method Post `
  -ContentType 'application/json' -Body '{"channel":"discord"}'
```

After changing `pc/wes_server.py`, the live server must be reloaded before
perf/eval/e2e reflect the change — see the `wes-reload` skill.
