# WES — Walrus Embedded Assistant

A 3-tier local voice assistant: **Pi 5 + Hailo-8** (edge: wake word, audio, on-device
vision) ↔ **PC / RTX 5060 Ti 16GB** (STT, LLM, TTS) ↔ **Claude API** (fallback).
Built and working today: wake word → VAD-endpointed capture → PC Whisper STT → local
**gemma4:e4b** via Ollama (`WES_LLM=local`; streaming + tools; Claude Haiku is the
fallback/optional backend) with **gemma4:12b** as the resident VLM for scene
description (both fit the 16GB card) → piper TTS streamed sentence-by-sentence to a
Bluetooth JBL. Turns share a sliding-window conversation memory (`docs/pipeline.md`).

> This root file is loaded every session — keep it lean. Put subsystem detail in
> `docs/*.md` and read those on demand. (`@import` does NOT save context; a pointer I
> choose to follow does.)

## Machines & layout

| Tier | Host | LAN IP | Role |
|------|------|--------|------|
| 1 | `raspberrypi` (alias `walrus-pi`) | 10.0.0.79 | Pi 5 + Hailo-8 — wake word, audio, vision |
| 2 | `DESKTOP-R2PFF9T` | 10.0.0.168 | PC (GTX 1660 SUPER) — STT, LLM, TTS |

- **`Z:\` on the PC is a Samba mount of the Pi**: `Z:\wes` = the Pi's
  `/home/walrus/claude/wes` (this repo). Editing `Z:\wes\*` edits the Pi's files. A
  Windows venv **cannot** live on `Z:\` — PC Python envs go on `C:`.
- The Pi also has a **separate** `~/wes` (NOT the repo): `~/wes/.venv` (Pi runtime
  venv), `~/wes/voices/`, `~/wes/known_faces.json`, `~/wes/logs/`. `~/cast-venv` is a
  catt/piper venv for casting.
- **System python3 vs venv**: Hailo + `cv2` code (`hailo_*.py`) needs the Pi's
  **system** `python3`; the client's wake-word/audio deps are in `~/wes/.venv`.

## Access & run

```bash
ssh walrus-pi        # alias → walrus@10.0.0.79, key ~/.ssh/walrus_pi
# Pi client (deps venv interpreter + repo script path):
~/wes/.venv/bin/python ~/claude/wes/pi/wes_client.py
```
```powershell
# PC server runs as scheduled task "WES Server" (auto-starts at logon).
Stop-ScheduledTask -TaskName "WES Server"; Start-ScheduledTask -TaskName "WES Server"  # reload after editing wes_server.py
Get-Content C:\Users\awarm\wes-pc\logs\server.log -Tail 20   # server log
```

## Key files

- `pi/wes_client.py` — wake word (openwakeword "hey_jarvis"), Silero-VAD endpointing,
  records the utterance, streams the reply to the JBL (`play_turn`), BT monitor + LED.
- `pi/hailo_faces.py` — SCRFD + ArcFace face detect/recognize + clothing tag (**system python3**).
- `pi/hailo_detect.py` — YOLOv8s object detection on the Hailo (**system python3**).
- `pi/pi_state.py` — read-only Pi state/vision endpoint on `:8090`.
- `pc/wes_server.py` — Flask `:8080`: Whisper STT, Claude (tools + vision), piper TTS.
- `pc/wes_discord.py` — Discord frontend: owner-allowlisted DMs → `POST /respond_text`
  (text-only, own conversation channel). Needs `WES_DISCORD_TOKEN` + `_OWNER_ID`
  (PC user env); run it manually or add a scheduled task (see `docs/setup.md`).
- `C:\Users\awarm\wes-pc\run_server.ps1` (PC-local, NOT in the repo) — the launcher
  the "WES Server" task runs; sets voice/model env. `tests/` — the test suite.

## Testing (do this every change)

Suite in `tests/` (full guide: `tests/README.md`). `$py = C:\Users\awarm\wes-pc\.venv\Scripts\python.exe`.
- Changed `pc/wes_server.py` → `& $py -m pytest Z:\wes\tests\test_unit_server.py -q`
- Changed `pi/hailo_faces.py` → `python3 ~/claude/wes/tests/test_faces.py` (on the Pi)
- Anything latency-affecting → `& $py Z:\wes\tests\perf_check.py` (flags regressions vs
  baseline; records to `tests/perf_history_stream.csv`)
- Anything reply-quality-affecting (prompts, routing, models, TTS) →
  `& $py Z:\wes\tests\eval_turns.py` (golden set vs live server + LLM judge —
  Haiku by default, `--judge local` for free gemma4:12b judging, `--judge
  both` to check their agreement; flags named-case regressions and
  judge-score drops; records to `tests/eval_history.csv`)
- E2E (real pipeline, costs a Claude call) → `& $py -m pytest Z:\wes\tests\test_e2e.py --run-e2e -q`
- **Add a test in the same change when you add a feature.** Keep unit tests
  hardware/API-free so they stay fast; reserve e2e for the full pipeline.

## Detailed docs (read on demand)

- `docs/pipeline.md` — the turn lifecycle: streaming `/respond_stream`, Silero-VAD
  endpointing, single-stream `play_turn`, speculative prefetch, telemetry, status LED.
- `docs/audio.md` — the speaker stack: JBL/Bluetooth pairing, the **A2DP
  persistent-silence** stability fix, Google Cast/`catt`, `speak.py`, output modes.
- `docs/vision.md` — on-device vision: Hailo `look` (YOLOv8s), Gemma `describe_scene`,
  face recognition + clothing-color disambiguation, wake-word vision prefetch.
- `docs/setup.md` — PC venv + dependency pins + auto-start task; Pi client + mic setup.
- `docs/hardware.md` — Pi/Hailo/camera specs (PERIPHERALS.md and PROJECT.md, both
  stale early-stage docs, were folded into this + the rest of `docs/` and removed).
- `docs/roadmap.md` — planned: smart home controls (Home Assistant), scheduled
  actions, barge-in; future Hailo use, storage, history. Discord remote access
  is built (pending bot credentials).
- `docs/eval-design.md` — design for the automated quality-eval harness (golden
  set + LLM-as-judge + nightly gate); phases 1-2 (`tests/eval_turns.py`,
  deterministic checks + Haiku judge) are built.
- `docs/memory-design.md` — long-term memory design (OpenClaw-style file-based
  MEMORY.md + remember/forget tools + nightly consolidation); exploration only,
  not yet built.
- `docs/keyresults.md` — current-cycle key results, refreshed periodically.
