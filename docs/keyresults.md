# Project-level key results

Reviewed and refreshed periodically (not every session) — a snapshot of what "good"
looks like right now, not a full backlog. The live task queue is `docs/tickets/`
(this file cross-references ticket ids).

> **This cycle is over, and three of its four KRs are now impossible.**
> KR2-KR4 were all voice/vision UX, and on 2026-09-02 the owner repurposed the
> Raspberry Pi that provided the microphone, speaker, camera and Hailo-8. Their
> tickets (#007, #008, #009) are closed in `done/2026-09/` as obsolete rather
> than shipped — see `archive/pi/README.md`.
>
> **Choosing the next cycle is still an owner decision, and it is not made
> here.** What the project has actually been doing since July is fantasy
> automation (#029, #035, #039, #040) and the data-layer work under it (#034,
> #037); the plausible next cycle is somewhere in that. This section is left
> describing the dead cycle honestly instead of being quietly replaced, because
> the replacement is a judgement call about direction, not bookkeeping.

## Retired cycle: UX polish (started 2026-07-03, ended 2026-09-02)

- **KR1 — Repo hygiene**: git-tracked, clean working tree, no orphaned files at root.
  **Done** (2026-07-03) — initial commit + cleanup landed same day.
- **KR2 — Barge-in** (ticket #007): interrupt a spoken reply by talking.
  **Obsolete** — there is no speaker to interrupt.
- **KR3 — Single-call scene description** (ticket #008): drop "what do you see"
  from 2 LLM calls to 1. **Obsolete** — there is no camera.
- **KR4 — Multi-person disambiguation** (ticket #009): clothing-colour
  disambiguation with 3+ people in frame. **Obsolete** — there is no face
  recognition.

The honest reading of the cycle is that it was never really started: the KRs sat
at the bottom of `open/low/` for two months while every actual change went to
the fantasy and data work. The hardware retirement did not cancel a cycle in
progress, it closed one that had already been abandoned in practice.
