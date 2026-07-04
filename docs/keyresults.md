# Project-level key results

Reviewed and refreshed periodically (not every session) — a snapshot of what "good"
looks like right now, not a full backlog. See `docs/roadmap.md` for the longer list of
built/not-yet-built ideas this draws from.

## Current focus: UX polish (~4-6 week horizon, started 2026-07-03)

- **KR1 — Repo hygiene**: git-tracked, clean working tree, no orphaned files at root.
  **Status: done** (2026-07-03) — initial commit + cleanup landed same day.
- **KR2 — Barge-in**: user can interrupt mid-reply by speaking and get a response
  within one turn cycle, no dead air. Feasible now that the A2DP persistent-silence
  fix keeps the transport open (`docs/audio.md`). **Status: not started.**
- **KR3 — Single-call scene description**: "what do you see" drops from 2 Claude/LLM
  calls to 1 by injecting the prefetched scene description directly into context
  instead of a tool round-trip. Measure via existing `X-Llm-Ms` / timing.csv.
  **Status: not started.**
- **KR4 — Multi-person disambiguation**: clothing-color disambiguation (`docs/vision.md`)
  verified correct with 3+ people in frame at once, not just the current 2-person
  (charlie/cindy) verification. **Status: not started.**

## Definition of done for this cycle

All four KRs checked off, `docs/roadmap.md` updated to move shipped items from "Ideas
noted but not built" into "Where things stand", and this file's focus section replaced
with the next cycle's picks.
