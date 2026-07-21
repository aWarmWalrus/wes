# WES — Walrus Embedded Assistant

A 3-tier local voice assistant: **Pi 5 + Hailo-8** (edge: wake word, audio, on-device
vision) ↔ **PC / RTX 5060 Ti 16GB** (STT, LLM, TTS) ↔ **Claude API** (fallback).
Built and working today: wake word → VAD-endpointed capture → PC Whisper STT → local
**gemma4:12b** via Ollama (`WES_LLM=local`; streaming + tools) serving as router,
escalation (thinking) and VLM in one → piper TTS streamed sentence-by-sentence to a
Bluetooth JBL. Turns share a sliding-window conversation memory (`docs/pipeline.md`).
Claude Haiku is the error-fallback/optional backend.

> **Model topology (2026-07-16): ONE model.** Was e4b-router + 12b-escalate; the
> e4b tag vanished from Ollama and every router call silently fell back to Claude
> for a week (`/health` echoes config, not reality). Collapsed to 12b alone as the
> project pivots to batch/analysis. Measured: 7.0GB weights, **7.8GB resident at
> `num_ctx=16384`** (~50MB KV per 1k ctx), 6.3GB free. `gemma3:4b` has vision but
> **no `tools`**, so it can never be the router. **Run `wes-dev.ps1 models check`
> after any model change** — it catches exactly this drift.

> This root file is loaded every session — keep it lean. Put subsystem detail in
> `docs/*.md` and read those on demand. (`@import` does NOT save context; a pointer I
> choose to follow does.)

## Machines & layout

| Tier | Host | LAN IP | Role |
|------|------|--------|------|
| 1 | `raspberrypi` (alias `walrus-pi`) | 10.0.0.79 | Pi 5 + Hailo-8 — wake word, audio, vision |
| 2 | `DESKTOP-R2PFF9T` | 10.0.0.168 | PC (RTX 5060 Ti 16GB) — STT, LLM, TTS |

> **`hosts.yaml` (repo root) is the single source of truth** for IPs and service
> ports — read at runtime via `wes_hosts.py` by the server, bot, and Pi client
> (env vars still override). Jarvis reads it through the `lookup_hosts` tool.
> Edit there when the network changes; this table and the ports below are a
> convenience mirror, not the authority.

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
# Pi client runs as a systemd USER unit (pi/wes-client.service, boot-persistent):
systemctl --user restart wes-client   # reload after editing pi/wes_client.py
journalctl --user -u wes-client -n 30 # logs
```
```powershell
# PC ops go through ONE allowlisted helper (no per-command prompt):
& C:\Users\awarm\wes-pc\wes-dev.ps1 <cmd>
#   reload [server|discord|exporters]  restart the task + wait for /health
#   test | eval [local|haiku] | perf   run the suites (see wes-test skill)
#   say <channel> <text> | reset | turns | usage | health | log [svc] [n]
#   gpu                                nvidia-smi + ollama ps
#   models [status|check|list|load|unload|fit]   VRAM/model manager (pin, drift-check)
# Raw equivalents (only if a flag the helper lacks is needed):
Stop-ScheduledTask -TaskName "WES Server"; Start-ScheduledTask -TaskName "WES Server"
Get-Content C:\Users\awarm\wes-pc\logs\server.log -Tail 20   # server log
```
> **Managing what's in VRAM:** `wes-dev.ps1 models status` shows resident/pinned
> models + config-vs-reality drift; `models load <tag>` pins a model
> (`keep_alive=-1`), `models unload` frees it, `models fit` checks headroom.
> Run `models check` after ANY model change — it catches the silent
> config/reality drift that once fell back to Claude for a week.

## Key files

- `pi/wes_client.py` — wake word (openwakeword "hey_jarvis"), Silero-VAD endpointing,
  records the utterance, streams the reply to the JBL (`play_turn`), BT monitor + LED.
- `pi/hailo_faces.py` — SCRFD + ArcFace face detect/recognize + clothing tag (**system python3**).
- `pi/hailo_detect.py` — YOLOv8s object detection on the Hailo (**system python3**).
- `pi/pi_state.py` — read-only Pi state/vision endpoint on `:8090`.
- `pc/wes_server.py` — Flask `:8080`: Whisper STT, Claude (tools + vision), piper TTS.
- `pc/wes_discord.py` — Discord frontend: owner-allowlisted DMs → `POST /respond_text`
  (text-only, own conversation channel). Runs as scheduled task "WES Discord"
  (`WES_DISCORD_TOKEN` + `_OWNER_ID` from PC user env; see `docs/setup.md`).
- `hosts.yaml` + `wes_hosts.py` (repo root) — the host registry (IPs/ports) and
  its loader; imported by server, bot, and Pi client. Jarvis reaches it via the
  `lookup_hosts` tool.
- `C:\Users\awarm\wes-pc\run_server.ps1` (PC-local, NOT in the repo) — the launcher
  the "WES Server" task runs; sets voice/model env. `tests/` — the test suite.

Project skills in `.claude/skills/`: **wes-test** (running the suites
correctly) and **wes-reload** (restarting the live services + reading logs) —
prefer them over re-deriving commands.

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
- `docs/tickets/` — the task tracker (one file per ticket). **`INDEX.md` lists
  open work by priority — read it for the current queue; don't load `done/`
  unless you need a shipped feature's history.** `docs/roadmap.md` is now just a
  short overview + project history that points here.
- `docs/observability.md` — Prometheus + Grafana utilization dashboard
  (<http://10.0.0.79:3000>): exporters on both hosts + `wes_server` `/metrics`
  (token counters) + `/turns` (recent-exchange log, size-capped `turns.jsonl`),
  dashboard provisioning, restart commands.
- `docs/eval-design.md` — design for the automated quality-eval harness (golden
  set + LLM-as-judge + nightly gate); phases 1-2 (`tests/eval_turns.py`,
  deterministic checks + Haiku judge) are built.
- `docs/memory-design.md` — long-term memory design (OpenClaw-style file-based
  MEMORY.md + remember/forget tools + nightly consolidation); exploration only,
  not yet built.
- `docs/fantasy-gm-design.md` — Fantasy GM epic (#029): autonomous Yahoo NBA
  team management (read → value → optimize → gated execute), per-team autonomy
  config + rails; phased roadmap. **P0 (Yahoo read) shipped 2026-07-20** —
  `fantasy_my_team` tool scrapes the owner's real roster + scoring via
  Playwright; P1 (valuation) is next. Platform pivoted from the official Yahoo
  API to browser automation (API access now requires a no-caching DocuSign).
- `docs/keyresults.md` — current-cycle key results, refreshed periodically.
