---
id: 024
title: SOUL.md — externalize Jarvis's identity/persona (OpenClaw pattern)
status: open
priority: med
created: 2026-07-06
closed:
tags: [memory, identity, persona, prompt, safety]
related: [docs/memory-design.md, "#012"]
---

## Goal
Move Jarvis's persona/voice/values out of the hardcoded `SYSTEM_PROMPT` into a
human-editable, evolvable `~/wes/memory/SOUL.md` injected every turn — the
identity layer of the memory architecture (`docs/memory-design.md`). It's the
OpenClaw pattern: read the "soul" first every wake, kept separate from MEMORY.md
(facts).

## Approach
- `SOUL.md` on the Pi (personal data, not the repo), always in the system
  prompt, unified across channels. The existing per-channel notes
  (`TEXT_CHANNEL_NOTE`, spoken framing) remain the *presentation* layer — soul
  unified, presentation per-channel (OpenClaw's soul-vs-identity split).
- Same injection plumbing as `MEMORY.md` (#012 Phase 1) — do them together.

## Safety (the important part)
- **Personality is soft; safety is hard.** Hard invariants — the house audio
  rule (never play audio without confirmation), owner-only Discord, invisible
  escalation, never reciting secrets — stay in **code/immutable config**, NOT in
  a mutable SOUL.md. Personality/values/voice/relationship stance only.
- SOUL.md is always-in-context + a prompt-injection/drift surface. If it ever
  becomes agent-editable, gate writes like MEMORY.md; keep it tight (bloated
  persona files degrade behavior).

## Acceptance
- [ ] SYSTEM_PROMPT persona externalized to SOUL.md; behavior unchanged in eval
- [ ] editing SOUL.md changes Jarvis's voice without code changes
- [ ] safety rules provably NOT sourced from SOUL.md (still enforced if it's
      emptied/tampered)
- [ ] full eval green (persona changes are quality-affecting)

## Notes
Ships with #012 Phase 1 (shared plumbing). Keep SOUL.md small — a paragraph or
two of who-Jarvis-is, not a novel.
