---
id: 023
title: Deepen + persist per-channel conversation memory (Discord gets a lot more)
status: open
priority: high
created: 2026-07-06
closed:
tags: [memory, discord, voice, working-memory]
related: [docs/memory-design.md, "#012", "#015"]
---

## Goal
Phase 0 of the memory architecture (`docs/memory-design.md` v2). Give **Discord
a lot more conversational memory** (both sides), make windows **survive
restarts**, and keep voice↔discord context from bleeding — WITHOUT the full
semantic/episodic layer (that's #012).

## Problem
Today's working memory (`_convs` in wes_server.py) is one policy for all
channels: RAM-only, `CONV_TURNS=6`, `CONV_TTL=300s`, wiped on restart. For
Discord that's far too little and too short (a chat spans hours/days), and a
restart forgets everything.

## Approach
- Make `CONV_TURNS` / `CONV_TTL` **per-channel** (e.g. discord = ~40 turns,
  long/no TTL; voice = 6 turns, 300s). Config map with a default.
- **Persist** each channel's window to `~/wes/memory/conversations/<channel>.jsonl`
  and rebuild it on startup (the raw `turns.jsonl` already has the data — can
  seed from it). Survives server restarts.
- For long windows, keep the last N verbatim + a rolling **summary** of older
  turns (LiveKit pattern) so depth doesn't blow the context budget. Summary
  refresh can reuse the local model off the hot path.
- **No bleed**: windows are strictly per-channel (already keyed that way);
  this ticket must not share window content across channels — only the durable
  layer (#012) is shared.

## Acceptance
- [ ] Discord recalls a fact from ~30 messages back, both sides
- [ ] windows survive a server restart
- [ ] a voice turn never surfaces Discord-only conversation content (no-bleed
      golden case)
- [ ] voice latency unchanged (short window preserved); perf_check green

## Notes
This is the user's principal near-term want (2026-07-06). Ships before #012.
Long-term semantic/episodic unification (people/events shared across channels)
is #012; read `docs/memory-design.md` for how they fit together.
