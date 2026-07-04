# WES tests

Regression tripwires + performance tracking. The system spans two hosts, so the
tests do too — run each host's set with that host's interpreter.

## PC (server logic + e2e + perf) — from the `wes-pc` venv

```powershell
$py = "C:\Users\awarm\wes-pc\.venv\Scripts\python.exe"

# Fast unit tests (no network/API/models) — run these after ANY server change:
& $py -m pytest Z:\wes\tests\test_unit_server.py -q

# End-to-end (real STT -> Claude -> TTS; needs the server running + API key):
& $py -m pytest Z:\wes\tests\test_e2e.py --run-e2e -q

# Performance check (records median latency, flags regressions vs history):
& $py Z:\wes\tests\perf_check.py            # 3 runs (default)
```

- `test_unit_server.py` — pure logic: `_vlm_prompt` (identity/clothing prompt),
  `next_sentence` (TTS sentence chunking), `_norm` + `lookup_speculation` (spec
  cache prefix match), rate-limit budget, `run_tool` dispatch, scene-cache TTL.
- `test_e2e.py` — `@e2e` (skipped unless `--run-e2e`): `/health`, and a piper→STT
  round-trip through `/respond` asserting the transcript + a spoken reply.
- `perf_check.py` — posts a fixed synthesized WAV to `/respond` N times, records
  the median `stt/llm/tts/total` ms to `perf_history.csv`, and exits **1** if a
  metric exceeds `median(recent) * 1.5 + 500ms`.

## Pi (vision / face pipeline) — SYSTEM python3 (needs cv2/hailo)

```bash
python3 ~/claude/wes/tests/test_faces.py
```

Covers the numpy/cv2 helpers: `match` (recognition), `_position`,
`_clothing_color`, `_distance2bbox`, `_nms`, and gallery I/O. No Hailo/camera
needed — the model inference paths are exercised by the ad-hoc `hailo_*` scripts.

## Conventions

- **After changing server logic** → run `test_unit_server.py`.
- **After changing `hailo_faces.py`** → run `test_faces.py` on the Pi.
- **After a change that could affect latency** → run `perf_check.py` and eyeball
  the diff vs baseline.
- **Adding a feature** → add a test for it in the same change.
- `WES_TEST_URL` overrides the server URL (default `http://127.0.0.1:8080`).
