# Roadmap & history

## Where things stand

Built and working: the full networked voice loop (wake word → VAD capture → PC STT →
local LLM streaming with tools → piper TTS → JBL), speculative prefetch, on-device
Hailo object detection + face recognition, and a two-host test suite including the
quality-eval harness (`docs/eval-design.md`, phases 1-2). The LLM tier is a **router**:
gemma4:e4b answers the easy tier itself and delegates — rich vision to the resident
gemma4:12b VLM, deep reasoning to Claude Haiku (with a server-injected escalation
acknowledgment masking Claude's spin-up). Keeping e4b as the router is evidence-backed
(A/B in `docs/pipeline.md`: 12b-as-router = +44-63% latency, no quality gain).

## Hybrid local inference — DONE (July 2026)

The GPU upgrade landed (RTX 5060 Ti 16GB; 64GB RAM coming). `WES_LLM=local` now runs
**gemma4:e4b** via Ollama as the primary LLM (streaming + tools + vision — one resident
model for both chat and `describe_scene`), with Claude as automatic fallback on local
errors and via `WES_LLM=claude`. Measured: llm ~706ms vs Haiku's ~1305ms; total turn
~2.6s vs ~4.0s. Smart routing is in too (`WES_ESCALATE`): gemma carries an
`escalate_to_claude` function and hands hard queries to Claude itself — see
`docs/pipeline.md`.

## Future Hailo use

- The Hailo currently does object detection (`look`) + face recognition (every wake
  word). Under-utilized headroom remains (26 TOPS).
- Candidates: pose/gesture as a tool (`yolov8s_pose` HEF is present), running detection
  on every turn for ambient context, moving wake-word detection to the Hailo (not
  supported by openwakeword today — would need a custom/alternative model).

## Smart home controls — planned (feasibility CONFIRMED 2026-07-04)

Give the router tools that *act*, not just observe. Natural fit for the existing
tool loop: e4b already routes tool calls well, and lights/switches/thermostat
commands are easy-tier (no escalation needed).

LAN discovery scan (mDNS + SSDP from the PC, 2026-07-04) found: **Philips Hue
Bridge** "Home" (10.0.0.143, BSB002, API 1.77 — local REST API, lights),
**ecobee thermostat** "Living Room" (10.0.0.155, ECB601 — local control only via
HomeKit protocol → needs Home Assistant), and four Cast devices (Chromecast Ultra
"Stevie" .106, Nest Hub Max "Kitchen Display" .130, plus the two off-limits
speakers Matcha .23 / Good gray .238). No existing HA instance, no Matter/Sonos/
Kasa/Shelly. Revised phasing:

1. **Hue-direct first, no HA needed**: one-time link-button pairing mints an API
   key (→ PC user env, like the Anthropic key), then two tools (`get_lights`,
   `set_light`) over plain local HTTP. *Pending: pressing the physical bridge
   button to pair.*
2. **Home Assistant later** for the ecobee + a unified layer — the only found
   device that actually requires it. The Pi 5 can host it, but watch RAM
   alongside the WES client; the PC is the fallback host.
3. Cast media control (pause/volume — not audio playback initiation for the
   off-limits speakers) via the existing catt/pychromecast stack.
- **Safety rails**: read-only states are free; *actions* should start with an
  allowlist of harmless domains (lights, switches, media players — **not** locks,
  garage doors, or anything security-relevant) and confirm-before-acting for
  anything outside it. Same spirit as the house audio rule.
- **Eval**: add golden cases per exposed action ("turn on the living room light" →
  exact tool + entity assertion — wants the phase-3 `X-Tools` header) with a mock
  HA endpoint so the eval never flips real devices.

## Scheduled actions — planned

Timers, alarms, reminders, and recurring routines ("remind me at 3", "every morning
tell me the weather").

- **Mechanics**: a `schedule_action` tool writing to a persisted store
  (`~/wes/schedules.json` on the Pi or a PC-side equivalent), plus a scheduler loop
  in an existing process (the Pi client already has a monitor thread) that fires the
  action through the normal reply path. One-shot + cron-style recurrence; list and
  cancel tools ("what are my reminders", "cancel that").
- **Audio-rule interaction**: a scheduled action *speaks later, unprompted*. Treat
  the user's explicit request as the confirmation for that one schedule (it plays on
  the JBL only — Matcha & Good gray stay off-limits), and announce-once rather than
  nagging. Anything WES wants to say on its own initiative (e.g. "nightly eval
  regressed") stays log-only per the house rule.
- **Depends on**: nothing hard — this can precede smart home controls, and once HA
  exists, scheduled actions compose with it ("turn off the lights at 11").

## Ideas noted but not built

- **Barge-in / interruption** (LiveKit-inspired): let the user cut the reply off by
  speaking — keep VAD/wake-word live during `play_turn` and kill the stream on speech.
  The persistent-silence transport makes this feasible. Highest-value UX gap.
- Inject the prefetched scene description directly into Claude's context to avoid the
  two-Claude-call tool round-trip for "what do you see".
- Storage tier on the PC for audio/video/transcripts (not yet built).
- Long-horizon memory: conversation memory across turns is DONE (see below); still
  open are persistence across server restarts and summarizing old turns instead of
  dropping them (LiveKit's background-summary pattern).

## Conversation memory — DONE (July 2026)

Server-side sliding-window chat context (`docs/pipeline.md` "Conversation
memory"): last `WES_CONV_TURNS` exchanges replayed to whichever backend answers,
shared across the Claude escalation handoff, idle-expired after `WES_CONV_TTL`,
`POST /reset_conversation` to clear. Verified e2e (fact recalled across turns
through the full audio loop) and gated by the `memory-recall` golden case; zero
measured latency cost. Unblocks multi-turn eval cases (`turns:` in golden.yaml)
and follow-up-listening on the Pi later.

## History (for context)

- `archive/wesley_cam.py` — the original self-contained Pi prototype (wake word →
  Whisper-tiny → email a photo). Superseded by the networked `pi/wes_client.py` +
  `pc/wes_server.py`. `archive/hand_filter.py` / `archive/finger_count.py` are earlier
  gesture-detection prototypes, also superseded (see `docs/vision.md` for the current
  Hailo vision stack).
- Wake word is "hey_jarvis" (openwakeword's only bundled model); a custom "hey_wesley"
  would need training — deferred.
- An earlier Anthropic API key was pasted in chat and treated as compromised; it was
  rotated 2026-07-03 (old key revoked, new key set via PC user env `setx`, verified
  working via the e2e suite). The key lives only in the PC user env, never the repo.
