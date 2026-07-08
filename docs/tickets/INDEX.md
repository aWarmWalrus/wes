# Open tickets

The current WES work queue — **open tickets only** (closed ones live in
`done/`, out of context). One line each; open the file for full context.
Conventions + workflow: `README.md`. Next free id: **028** (open here;
001 + 014-024 shipped in `done/2026-07/`).

## High priority

- [002](open/high/002-escalation-announced-not-executed.md) — Router promises to "look into it" but never calls `escalate_to_claude` (no deferred-action support)

## Medium priority

- [003](open/med/003-voice-tool-turn-latency.md) — Speak a filler before slow tool turns (describe_scene miss = ~18s to first audio)
- [004](open/med/004-smart-home-controls.md) — Smart home tools: Hue-direct first, Home Assistant later (feasibility confirmed)
- [005](open/med/005-scheduled-actions.md) — Scheduled actions: timers, reminders, recurring routines
- [012](open/med/012-durable-agentic-memory.md) — Unified durable memory: Phase 1 (MEMORY.md + remember/forget) shipped; remaining = nightly consolidation, temporal facts, per-person notes
- [026](open/med/026-adaptive-thinking-budget-router.md) — Adaptive thinking budget: router allocates effort per query + verify-and-escalate net (SotA-grounded; supersedes #001's "always 12b on Discord")
- [027](open/med/027-nba-domain-internet-access.md) — NBA domain expertise: MCP (balldontlie/reddit/fetch) + local nightly-refreshed cache + live scores/news/discussions

## Low priority / someday

- [006](open/low/006-observability-phase-3b.md) — Observability 3b: turn-latency histograms on /metrics + more alert rules
- [007](open/low/007-barge-in-interruption.md) — Barge-in: cut the reply off by speaking mid-playback
- [008](open/low/008-single-call-scene.md) — Inject prefetched scene into context to skip the vision tool round-trip (KR3)
- [009](open/low/009-multi-person-disambiguation.md) — Verify clothing-color disambiguation with 3+ people in frame (KR4)
- [010](open/low/010-future-hailo-use.md) — Use spare Hailo headroom: pose/gesture, ambient detection, wake-word on-device
- [011](open/low/011-eval-flakes.md) — Eval flakes: lexicon-names STT + math-simple arithmetic
- [013](open/low/013-storage-tier.md) — Storage tier on the PC for audio/video/transcripts
- [025](open/low/025-dashboard-gpu-temp-power-axis.md) — Dashboard "GPU temperature / power" panel mixes °C and W on one axis (bug)
