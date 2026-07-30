# Roadmap

> **Task tracking moved to `docs/tickets/` on 2026-07-06.** This file is now
> just a short living overview; the itemized queue (open work by priority) and
> the archive of shipped work (with full per-item context) live as one markdown
> file per ticket. Start at **`docs/tickets/INDEX.md`** — it lists only open
> tickets, so completed work no longer costs reading context. Conventions:
> `docs/tickets/README.md`.

## Where things stand

Built and working: the full networked voice loop (wake word → VAD capture → PC
STT → local LLM streaming with tools → piper TTS → JBL), speculative prefetch,
on-device Hailo object detection + face recognition, per-channel conversation
memory, a Discord remote frontend, a two-host test suite with the quality-eval
harness, host + app observability (Prometheus/Grafana, token metrics, turn log)
with Jarvis DM-ing alerts, and a `hosts.yaml` registry. The LLM tier is a
**router**: **gemma4:12b** (one model since 2026-07-16, was e4b-router+12b)
answers the easy tier itself and delegates — vision via the same 12b VLM, deep
reasoning via the same 12b with thinking (`WES_ESCALATE_MODEL`, fully local
since 2026-07-05); Claude Haiku is error-fallback only. Details in
`docs/pipeline.md`.

## Current focus

The live queue is `docs/tickets/INDEX.md` — read it there, not here. As of
2026-07-29 the high-priority items are boot/service reliability (#032, from the
2026-07-28 silent 24h outage), the Fantasy GM epic (#029, P0-P2 engine shipped,
in-season work pending) and the planner (#028, Option A shipped). The three
turn-log issues that headed this list on 2026-07-06 — Discord vision
hallucination (#001), unexecuted escalation promises (#002), voice tool-turn
latency (#003) — all shipped and live in `done/2026-07/`.

Session hygiene: `docs/startup-checklist.md` — verify services, pins, and
metrics freshness before trusting the system's state.

## History (project context, not tasks)

- `archive/wesley_cam.py` — the original self-contained Pi prototype (wake word →
  Whisper-tiny → email a photo), superseded by the networked client+server.
  `archive/hand_filter.py` / `finger_count.py` are earlier gesture prototypes,
  superseded (see `docs/vision.md`).
- Wake word is "hey_jarvis" (openwakeword's only bundled model). The assistant's
  spoken identity was renamed Wesley → **Jarvis** to match it (2026-07-04). Bare
  "Jarvis" scores lower; the client logs near misses and `WES_WAKE_THRESHOLD`
  tunes it.
- An earlier Anthropic API key was pasted in chat and treated as compromised;
  rotated 2026-07-03 (old revoked, new set via PC user env `setx`, verified via
  the e2e suite). Secrets live only in the PC user env, never the repo.

Shipped feature history now lives as closed tickets in `docs/tickets/done/`.
