# Long-term memory design (exploration 2026-07-04 — nothing built yet)

Goal: grow Jarvis from a turn-taking assistant into a broader agentic one —
which above all means **remembering across days**: who lives here, standing
preferences ("Kaia's bedtime is eight"), house facts, running errands. Today's
conversation memory (`pipeline.md`) is a RAM-only sliding window: 6 exchanges,
gone after 5 idle minutes or a server restart. That's *working* memory; this
doc is about everything longer-lived.

## The four memory kinds (standard taxonomy)

| kind | what it is | WES today |
|------|-----------|-----------|
| working | the current conversation | ✅ sliding window |
| semantic | durable facts (people, preferences, the house) | ❌ |
| episodic | what happened when ("what did I ask yesterday") | ❌ (timing.csv is metrics, not content) |
| procedural | how to do recurring things | prompt + code only |

## Landscape (July 2026)

- **OpenClaw-style file-based memory** — the pattern behind the agentic
  assistants the user is pointing at: *"if it's not written to a file, it
  doesn't exist."* Plain Markdown: a curated `MEMORY.md` always in context,
  dated session logs for episodic history, the agent reads/writes its own
  memory with ordinary file tools. No database, greppable, human-auditable.
- **Letta (MemGPT)** — context-as-virtual-memory: an in-window core the agent
  self-edits + vector-searched recall/archival tiers it pages in. The right
  model for agents whose memory far exceeds context.
- **Mem0 / Zep** — memory as a service: extract facts from conversations into
  a vector store (Zep: a temporal knowledge graph), retrieve per turn.
  Managed infra, framework-agnostic.
- **LiveKit summary pattern** — background-summarize old turns instead of
  dropping them (already noted in `pipeline.md` as the working-memory growth
  path).

## Recommendation: OpenClaw-style files, WES-shaped

For a single household on local hardware, file-based wins on every axis that
matters here:

- **Scale**: a house's durable facts are kilobytes. e4b can carry the whole
  `MEMORY.md` in its system prompt every turn — no retrieval step, no vector
  DB, no new resident process competing for the 16GB card.
- **Auditability**: a home assistant's memory should be something the family
  can `cat`, edit, and delete. Markdown in the repo-adjacent Pi home dir is
  exactly that. (Vector stores fail this: you can't easily see what it
  "knows" about you.)
- **Fit**: the router already does tool-calling; `remember` / `forget` are
  two more tools. And eval phase 4's `turns.jsonl` (planned) doubles as the
  episodic layer for free.
- Letta/Mem0-style retrieval becomes worth it only if memory outgrows the
  context budget — that's a later layer on top, not a different foundation.

### Proposed architecture

```
~/wes/memory/MEMORY.md      curated semantic memory — ALWAYS in the system
                            prompt (size-capped ~2-3KB); one fact per line
~/wes/memory/log/YYYY-MM-DD.md   episodic: distilled per-day notes
pc turns.jsonl (phase-4 eval)    raw episodic feed (rotated, text-only)
```

- **Read path**: `SYSTEM_PROMPT + MEMORY.md + scene + conversation window`.
  Also: household names in MEMORY.md feed `WES_STT_LEXICON` automatically —
  memory improves *hearing*, not just answers.
- **Write path A (explicit)**: a `remember` tool the router calls when the
  user says remember/my-X-is/from-now-on. Append one line with a date.
  `forget` removes by content match; "what do you remember" reads it aloud.
- **Write path B (consolidation)**: nightly (the "WES Nightly Eval" slot),
  the local 12b distills the day's `turns.jsonl` into candidate facts +
  a one-paragraph day log — the sleep-consolidation pattern. Candidates go
  to a review section of MEMORY.md, not straight to the trusted top.
- **Restart-proof and reset-proof**: `/reset_conversation` clears the window
  only; long-term memory survives server restarts (unlike today's window).

### Security & hygiene (the part OpenClaw gets criticized for)

Everything in MEMORY.md is executed as trusted context forever — memory is a
prompt-injection surface. Rules: memory writes only via the two gated paths
(explicit user intent, or nightly distillation into a review section); never
store verbatim tool output, URLs, or anything that looks like an instruction;
hard size cap with oldest-reviewed-first eviction; the file lives on the Pi
(user-ownable), never in the git repo (it's personal data — same class as
API keys).

### Eval story

New golden cases: remember a fact → `/reset_conversation` → recall (proves
it's long-term, not the window); recall after a server restart (manual/e2e);
"forget X" → gone; nightly-distillation output judged by the local judge.
`memory-recall` (window) stays as-is — the two layers gate separately.

## Build order

1. `MEMORY.md` injection + `remember`/`forget`/`list` tools + golden cases.
   Smallest useful slice; makes "remember that Kaia's bedtime is eight" work
   and survive a restart.
2. `turns.jsonl` (shared with eval phase 4) + nightly distillation into the
   day log + review-section candidates.
3. Names/devices in MEMORY.md auto-feed the STT lexicon.
4. Retrieval layer (embeddings on the GPU, or memsearch-style) ONLY if
   MEMORY.md pressure ever exceeds the context budget — measure first.
