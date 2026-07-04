# Roadmap & history

## Where things stand

Built and working: the full networked voice loop (wake word → VAD capture → PC STT →
Claude streaming with tools + vision + face recognition → piper TTS → JBL), speculative
prefetch, on-device Hailo object detection + face recognition, local Gemma VLM scene
description, and a two-host test suite. Primary LLM is local gemma4:e4b via Ollama
(Claude Haiku is the fallback / optional backend).

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

## Ideas noted but not built

- **Barge-in / interruption** (LiveKit-inspired): let the user cut the reply off by
  speaking — keep VAD/wake-word live during `play_turn` and kill the stream on speech.
  The persistent-silence transport makes this feasible. Highest-value UX gap.
- Inject the prefetched scene description directly into Claude's context to avoid the
  two-Claude-call tool round-trip for "what do you see".
- Persistent conversation context / memory across turns.
- Storage tier on the PC for audio/video/transcripts (not yet built).

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
