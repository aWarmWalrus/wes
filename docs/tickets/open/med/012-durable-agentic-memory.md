---
id: 012
title: Unified durable memory — semantic/episodic, shared across channels
status: open
priority: med
created: 2026-07-04
closed:
tags: [memory, agentic, cross-channel]
related: [docs/memory-design.md, "#023", "#015", "#011"]
---

## Goal
The **unified** (channel-agnostic) durable layer from the v2 memory architecture
(`docs/memory-design.md`): semantic facts about people/preferences/the house +
episodic day-logs, written from ANY channel and readable in EVERY channel — so a
fact told to Discord is known on voice. Per-channel conversation depth is the
separate #023; this is the shared knowledge underneath it.

## Approach (Phases 1-4 of the design doc)
File-based (Obsidian/Karpathy-wiki-shaped), chosen over vector/Zep/Mem0/OpenBrain
infra — a household's facts are kilobytes and should be `cat`-able/deletable.
- **P1** `MEMORY.md` injection (always in prompt, size-capped) +
  `remember`/`forget`/`recall` tools + golden cases (cross-channel + no-bleed).
- **P2** nightly "dream": distill `turns.jsonl` into day-logs + confidence-scored
  candidate facts into a review section (never straight to trusted).
- **P3** temporal/supersedable facts (Zep's idea in Markdown) + per-entity
  `people/<name>.md` notes; names auto-feed `WES_STT_LEXICON` (helps #011).
- **P4** embedding retrieval ONLY if measured context pressure demands it.

## Acceptance
- [ ] remember/forget/recall persist facts across sessions + restarts
- [ ] a fact learned on Discord is recalled on voice (cross-channel golden case)
- [ ] Discord-only chatter does NOT surface on voice (no-bleed golden case)
- [ ] nightly consolidation produces reviewed day-logs + candidate facts

## Notes
Read `docs/memory-design.md` (v2, 2026-07-06) first — the architecture, the
channel split, the file-based-vs-SotA comparison, and build order are all there.
Security: memory is a prompt-injection surface — gated writes, size cap, lives
on the Pi not the repo. Depends on nothing hard; #023 (Phase 0) ships first.
