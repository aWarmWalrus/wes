# Roadmap

> **Task tracking moved to `docs/tickets/` on 2026-07-06.** This file is now
> just a short living overview; the itemized queue (open work by priority) and
> the archive of shipped work (with full per-item context) live as one markdown
> file per ticket. Start at **`docs/tickets/INDEX.md`** — it lists only open
> tickets, so completed work no longer costs reading context. Conventions:
> `docs/tickets/README.md`.

## Where things stand

Built and working: a Discord frontend, per-channel conversation memory with
durable facts, an autonomous fantasy GM that reads, values and really writes
rosters on Yahoo and Sleeper (including drafting), a quality-eval harness with
an LLM judge, observability (Prometheus/Grafana, token metrics, turn log) with
Jarvis DM-ing alerts, and a `hosts.yaml` registry. The LLM tier is a **router**:
**gemma4:12b** (one model since 2026-07-16, was e4b-router+12b) answers the easy
tier itself and escalates to the same 12b with thinking
(`WES_ESCALATE_MODEL`, fully local since 2026-07-05); Claude Haiku is the error
fallback and the web-search tier.

**The voice half is retired.** Until 2026-09-02 the headline feature was a full
networked voice loop — wake word → VAD-endpointed capture → PC Whisper STT →
streaming local LLM → piper TTS → a Bluetooth JBL — with speculative prefetch,
on-device Hailo object detection and face recognition. The owner repurposed the
Raspberry Pi that made all of it possible, so it came out of the running system
in one change. `archive/pi/README.md` maps what went where; the design docs are
in `docs/archive/`.

## Current focus

The live queue is `docs/tickets/INDEX.md` — read it there, not here. As of
2026-09-02 the centre of gravity is fantasy: the Sleeper platform work (#039,
with a real draft on 2026-09-04), roster management (#035) and the data-layer
and valuation interfaces under them (#034, #037). Service reliability (#032) is
still open and is now really "make a dead service loud" — its original Z:-drive
cause went with the Pi. The three turn-log issues that headed this list on
2026-07-06 — Discord vision hallucination (#001), unexecuted escalation promises
(#002), voice tool-turn latency (#003) — all shipped and live in `done/2026-07/`.
Four voice/vision tickets (#007-#010) were closed as **obsolete rather than
shipped** in `done/2026-09/` when the hardware went.

Session hygiene: `docs/startup-checklist.md` — verify services, pins, and
metrics freshness before trusting the system's state.

## History (project context, not tasks)

- `archive/pi/` — the whole retired tier-1 stack (2026-09-02): wake-word client,
  Hailo face/object vision, the `:8090` state endpoint, the cast utility.
- `archive/wesley_cam.py` — the original self-contained Pi prototype (wake word →
  Whisper-tiny → email a photo), superseded by the networked client+server.
  `archive/hand_filter.py` / `finger_count.py` are earlier gesture prototypes,
  superseded (see `docs/archive/vision.md`).
- The name: wake word was "hey_jarvis" (openwakeword's only bundled model), and
  the assistant was renamed Wesley → **Jarvis** to match it (2026-07-04). The
  wake word is gone; the name stuck, which is why an assistant with no
  microphone is still called Jarvis.
- An earlier Anthropic API key was pasted in chat and treated as compromised;
  rotated 2026-07-03 (old revoked, new set via PC user env `setx`, verified via
  the e2e suite). Secrets live only in the PC user env, never the repo.

Shipped feature history now lives as closed tickets in `docs/tickets/done/`.
