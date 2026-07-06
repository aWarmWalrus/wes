# Memory architecture — unified across channels (design v2, 2026-07-06)

Supersedes the v1 exploration (2026-07-04). v1 correctly picked file-based
memory; v2 adds the piece the user asked for: a **channel axis** that keeps
conversational context from bleeding between voice and Discord while unifying
the durable knowledge about people and events — plus a comparison against
Obsidian / OpenBrain / SotA memory stacks and a phased build plan.

Nothing here is built yet beyond today's working memory and the turn log.

## The core idea: two axes, not one

Memory splits two ways *at the same time*:

- **By durability** (standard taxonomy): working · semantic · episodic · procedural.
- **By channel**: voice · discord · (future: SMS, web, etc.).

The realization that resolves the seemingly-conflicting requirements ("more
Discord memory", "don't bleed voice↔discord", "but unify people/events"): **the
two axes are orthogonal, and each memory kind lives at a different point on the
channel axis.**

| Layer | Kind | Channel scope | WES today |
|-------|------|---------------|-----------|
| Identity ("soul") | *who Jarvis is*: persona, voice, values | **unified** (presentation adapts per channel) | static `SYSTEM_PROMPT` in code |
| Conversation window | working | **per-channel** (must NOT bleed) | RAM window, 6 turns, 5-min TTL, lost on restart |
| Semantic memory | facts: people, prefs, house | **unified** (shared everywhere) | ❌ |
| Episodic memory | what happened when | **unified** (shared everywhere) | `turns.jsonl` = raw feed, no distillation |
| Procedural | how to do recurring things | unified | prompt + code |

So "don't bleed" and "unify" are **not in tension** — they apply to different
layers. Conversational context stays private per channel (your kitchen voice
chatter ≠ your Discord thread); the distilled durable knowledge is shared. Voice
won't parrot your Discord small-talk, but it *will* know the facts you told
Discord ("Kaia is my sister"). That split is the whole design.

## Landscape — what the field is doing (2026)

Two families, and WES should take the cheap ideas from the heavy one.

### File-based / "second brain" (plain Markdown, human-owned)
- **Karpathy's LLM Wiki pattern** — raw sources in `raw/`, an LLM "compiles" a
  wiki of `.md` files with summaries + backlinks + a graph view; Obsidian is
  just the viewer. The dominant open pattern in 2025-26.
- **Obsidian second-brain agents** (claude-obsidian, second-brain, open-second-brain)
  — LLM reads/links/files plain Markdown into a connected graph via `[[wikilinks]]`;
  no DB, no lock-in, `grep`-able, git-versioned. `open-second-brain` adds a
  nightly **"dream" pass** that promotes repeated corrections into confirmed
  preferences with a **confidence score** — directly relevant to WES.
- **OpenClaw file memory** — "if it's not written to a file, it doesn't exist";
  a curated always-in-context file + dated session logs. WES's *own* agent
  memory (`MEMORY.md` + `[[links]]`) already works exactly this way.

### Retrieval services (databases, embeddings, MCP)
- **OpenBrain** — Postgres + **pgvector**, provider-agnostic, exposes memory to
  any LLM over **MCP**; semantic search instead of curation. Powerful, but it's
  a database service and its knowledge isn't human-auditable at a glance.
- **Mem0** — vector + graph + key-value with automatic fact extraction; the
  popular drop-in personalization layer (managed infra).
- **Zep / Graphiti** — a **temporal knowledge graph**: every fact is timestamped
  with a validity window, so "Kaia's bedtime *was* eight, now it's nine" is
  modeled as supersedence instead of two conflicting values. Best benchmark
  accuracy (LongMemEval 63.8% vs Mem0 49%). The idea is gold; the infra is heavy.
- **Letta / MemGPT** — OS-inspired tiers (core in-context + recall + archival);
  the agent pages memory in/out like RAM↔disk. The right model *only* once
  memory dwarfs the context window.

### Verdict for WES
A single household's durable facts are **kilobytes** — they fit in e4b's context
every turn with room to spare, so retrieval (vectors, pgvector, MCP services)
solves a problem WES doesn't have yet, at the cost of a new resident process
competing for the 16GB GPU and — worse — memory a family can't `cat` or delete.
**Stay file-based (Obsidian/Karpathy-shaped), and borrow the good ideas from the
graph/temporal camp without their infra:** timestamped/supersedable facts (Zep),
nightly confidence-scored consolidation (open-second-brain), and a self-managed
"what's important enough to keep in context" tier (Letta), all expressed as
plain Markdown. Add real retrieval only if measured context pressure ever
demands it (Phase 4).

## Proposed architecture

```
Pi ~/wes/memory/                     (personal data — on the Pi, NOT the git repo)
  SOUL.md              unified IDENTITY: persona, voice, values, relationships;
                       always in prompt. Soft/evolvable — hard SAFETY rules
                       stay in code (see "Identity layer" below), NOT here.
  MEMORY.md            unified SEMANTIC memory: one dated fact/line, [[links]];
                       ALWAYS in the system prompt (size-capped ~2-3 KB)
  people/<name>.md     per-entity notes (kaia, charlie, …) — Obsidian-style,
                       linked from MEMORY.md; loaded on demand / when in-scene
  log/YYYY-MM-DD.md    unified EPISODIC memory: distilled per-day notes
  conversations/<channel>.jsonl   per-channel WORKING memory, persisted

PC turns.jsonl (built) raw cross-channel episodic feed -> nightly distillation
```

### Identity layer — `SOUL.md` (the OpenClaw pattern)
OpenClaw agents read a `SOUL.md` first every wake — persona, values, tone,
behavioral philosophy — kept separate from `MEMORY.md` (facts). WES's
`SYSTEM_PROMPT` is a static, in-code proto-soul; externalizing it to a
`SOUL.md` makes Jarvis's identity human-editable and *evolvable* (it can grow
as the household relationship does) without code changes, consistent with the
file-based philosophy.

Two design rules make this safe for a home assistant with physical side effects:

1. **Soul is unified; presentation is per-channel.** OpenClaw splits "soul"
   (what the model embodies) from "identity" (what users see). That maps exactly
   onto WES: one `SOUL.md` across all channels, while the existing per-channel
   notes (`TEXT_CHANNEL_NOTE` for Discord, spoken framing for voice) stay as the
   *presentation* adjustment. Jarvis is the same person on voice and Discord; he
   just types vs. speaks.
2. **Personality is soft and evolvable; safety is hard and stays in code.** The
   house audio rule (never play audio without confirmation), owner-only Discord,
   invisible escalation, and the never-recite-secrets rules are **invariants, not
   personality** — they must NOT live in a mutable, possibly self-edited
   `SOUL.md` (memory/soul files are a prompt-injection + drift surface, and
   they're always in context). Keep them in code/immutable config; `SOUL.md`
   holds only voice, values, humour, and relationship stance. If `SOUL.md` ever
   becomes agent-editable, gate writes like `MEMORY.md` and keep it tight —
   bloated persona files measurably degrade behavior.

### Channel model (the crux)
- **Working memory = per-channel, persisted, sized per channel.** Replace the
  single RAM window with per-channel windows rebuilt from
  `conversations/<channel>.jsonl` (so they survive restarts). Config becomes
  per-channel: **Discord = deep + long-lived** (e.g. 40 turns, hours/days TTL),
  **voice = shallow + short** (e.g. 6 turns, minutes) since spoken context ages
  out and latency matters. When a channel's window is long, keep the last N
  verbatim + a rolling **summary** of older turns (the LiveKit pattern) so depth
  doesn't blow the context budget.
- **Semantic + episodic = unified.** `MEMORY.md` and the day-logs are injected
  regardless of channel and written by channel-agnostic tools. A fact learned on
  Discord is known on voice next time.

### Read path (per turn)
`system_prompt(channel)` = `SYSTEM_PROMPT + channel_note + MEMORY.md +
in-scene people notes + scene` … then `+ conversation_window(channel)`. The
durable layer is shared; only the window varies by channel.

### Write paths
- **Explicit** — `remember` / `forget` / `recall` tools the router calls on
  "remember that…", "my X is…", "what do you know about Kaia". Appends a dated
  line (or an entity note) to `MEMORY.md`. Unified: callable from any channel.
- **Consolidation ("dream")** — nightly (the "WES Nightly Eval" slot), the 12b
  distills the day's `turns.jsonl` into (a) a day-log and (b) candidate facts
  with a **confidence** tag. Candidates land in a *review* section of
  `MEMORY.md`, promoted to trusted only after repetition — never straight in.
- **Temporal** — facts carry a date; a superseding fact marks the old one stale
  (Zep's idea, in Markdown) rather than contradicting it.

### Bonus: memory improves *hearing*
Household names in `MEMORY.md`/`people/` auto-feed `WES_STT_LEXICON`, so
remembering "Kaia" also fixes the lexicon-name STT flake (ticket #011).

## Security & hygiene
Everything in `MEMORY.md` is trusted context forever — memory is a
prompt-injection surface. Rules: writes only via the gated paths (explicit user
intent, or reviewed consolidation); never store verbatim tool output, URLs, or
anything shaped like an instruction; hard size cap with oldest-reviewed-first
eviction; the file lives on the Pi (family-ownable, `cat`/edit/delete-able),
never in the git repo — same class as API keys.

## Eval story
New golden cases: remember a fact → `/reset_conversation` → recall (proves it's
long-term, not the window); recall after a server restart (e2e); "forget X" →
gone; cross-channel (remember on `text`, recall on `voice`); a **no-bleed**
case (Discord-only chatter must NOT surface in a voice turn). Consolidation
output judged by the local judge. The window `memory-recall` case stays as-is —
the layers gate separately.

## Phased plan (maps to tickets)

- **Phase 0 — deepen Discord working memory (quick win).** Make `CONV_TURNS` /
  `CONV_TTL` per-channel; persist windows to `conversations/<channel>.jsonl` so
  they survive restarts; Discord gets a big long-lived window, voice stays
  short. No new architecture — this alone delivers "a LOT more Discord memory"
  and the no-bleed guarantee. → ticket #023.
- **Phase 1 — unified semantic memory.** `MEMORY.md` injection +
  `remember`/`forget`/`recall` tools + golden cases (incl. cross-channel +
  no-bleed). → ticket #012. Good moment to also externalize `SOUL.md` (identity
  layer) since it's the same injection plumbing — but keep hard safety rules in
  code. → ticket #024.
- **Phase 2 — episodic + nightly "dream".** Distill `turns.jsonl` into day-logs
  + confidence-scored candidate facts. → ticket #012.
- **Phase 3 — temporal facts + per-entity `people/` notes** (Obsidian-style
  links; feeds STT lexicon). → ticket #012.
- **Phase 4 — retrieval layer** (embeddings on the GPU) *only if* measured
  `MEMORY.md`/context pressure ever exceeds budget. Not before.

## Sources
Karpathy LLM-wiki / Obsidian second-brain pattern; `open-second-brain` (nightly
dream + confidence); OpenBrain (pgvector + MCP); Mem0; Zep/Graphiti (temporal
KG); Letta/MemGPT (tiered). Links in the session that produced this doc.
