---
id: 027
title: NBA domain expertise via MCP + local cache (live scores, news, discussions)
status: open
priority: med
created: 2026-07-07
closed:
tags: [nba, mcp, internet, tools, cache, security]
related: [docs/memory-design.md, "#004", "#005", "#026"]
---

## Goal
Make Jarvis feel like an NBA domain expert with live data: scores, scheduled
games, player/team data, and current discussion from r/nba + team subreddits +
Yahoo news. Use MCP servers where a good one exists; keep oft-accessed reference
data (teams, rosters, standings, records) cached locally and refreshed nightly.

## Key reality
gemma's NBA knowledge is frozen at its training cutoff (wrong standings, misses
trades). The domain-expert feel MUST come from live/cached data tools, not the
model's memory.

## Owner requirements (2026-07-07)
- Must answer LIVE in-game queries: "what's the score right now", "how many
  points has <player> scored so far in the third quarter". Both channels.
- Team = **Brooklyn Nets**; start with **r/GoNets** for discussion.
- Free first — no balldontlie key yet; don't pay until we confirm we must.

## DECISION (research 2026-07-07): use ESPN's free API, not balldontlie
- **balldontlie free tier is too limited** for this: teams/players/games only;
  **player stats + standings require PAID** — so it can't do live per-player
  points for free anyway.
- **ESPN hidden API (free, NO key, no signup)** covers BOTH owner queries:
  - scoreboard `site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard`
    → live scores + status (quarter/clock), today or `?dates=YYYYMMDD`.
  - summary `site.web.api.espn.com/apis/site/v2/sports/basketball/nba/summary?event={id}`
    → live box score incl. per-player points.
  Caveat: unofficial/undocumented — can break without notice; handle failures
  gracefully, revisit paid balldontlie only if it gets flaky. Skip balldontlie
  for now.

## Data sources (verified 2026-07-07)
- **BALLDONTLIE** (balldontlie.io) — teams/players/games/box_scores/standings/
  injuries. Free tier covers schedule/standings/rosters/box scores; **real-time
  live scores need the paid tier (~$9.99/mo ALL-STAR)**. Official MCP server:
  github.com/balldontlie-api/mcp (250+ endpoints — MUST curate what we expose).
- **Reddit** — zero-config, no-auth MCP servers exist
  (github.com/eliasbiondo/reddit-mcp-server): subreddits, hot posts, comments.
  (Reddit's public *.json endpoints are a direct fallback.)
- **News** — official Fetch + Brave Search MCP servers (Brave needs an API key);
  Yahoo NBA has RSS we can pull directly.

## Architecture (4 layers)
1. **MCP client layer in wes_server** — WES becomes an MCP client (python `mcp`
   SDK), connects to configured servers (balldontlie, reddit, fetch/search),
   lists their tools, and bridges them into the Ollama tool loop (translate MCP
   tool schema → TOOLS format; route calls to the server). The reusable piece —
   pays off beyond NBA (smart home #004, etc.).
2. **Curated tool exposure** — do NOT expose 250 balldontlie endpoints to the
   small router. Whitelist a handful (games, standings, players, box_scores) OR
   wrap NBA behind ONE parameterized `nba` tool. Router tool-count is already ~10.
3. **Local NBA cache + nightly updater** — standings, schedule, rosters, records
   into `~/wes-pc/nba/` (JSON or sqlite), refreshed by a nightly scheduled task
   (the eval slot pattern). Common queries instant/offline; spares rate limits.
4. **On-demand live fetch** — scores, breaking news, subreddit threads fetched
   fresh + summarized.

## Security (important — new internet + untrusted-content surface)
- **Prompt injection**: reddit/news content becomes model context, and WES has a
  `remember` write path + (soon) house actions. Treat ALL fetched web content as
  untrusted QUOTED DATA, never instructions; block it from triggering
  `remember`/`forget`/actions. Frame it explicitly in the prompt.
- **Keys** (balldontlie, Brave) → PC user env via setx, never the repo.
- **Rate limits / caching**: cache aggressively; the nightly updater does the
  bulk pulls, on-demand is sparing.
- Outbound only, to the specific known hosts.

## Open decisions (gate the build)
- MCP client layer now (reusable, bigger lift) vs direct REST tools first (faster)?
- balldontlie free tier (no live real-time) vs paid ALL-STAR (~$10/mo live scores)?
- scope of subreddits/news sources (tunable later; not gating).

## Acceptance
- [x] "what's the score of the Celtics game" → live/last data via tool, not guess (P1)
- [x] "how many points has <player> scored so far" → live box score (P1)
- [x] "what games were played on May 20th" → real results via tool (P1)
- [x] curated tool surface — no router-quality regression in the eval (14/14, P1)
- [x] "what's r/GoNets saying" → summarized real threads via RSS (P1b)
- [x] fetched web content guarded as untrusted (injection test) (P1b)
- [ ] "when do the Warriors play next" → schedule lookup (needs #028 / P2 cache)
- [ ] nightly cache refresh runs as a scheduled task (P2)

## P1 — SHIPPED 2026-07-07
- `pc/wes_nba.py`: ESPN free-API client (no key). `live_scores(team, date)` +
  `player_points(player)`; short TTL cache; every entry point degrades to a
  soft "couldn't reach the NBA data" string so a bad ESPN response never breaks
  a turn. Pure formatters/matchers/date-parser split out for deterministic tests.
- Two curated tools in `wes_server.TOOLS` (kept to 2 so the e4b router isn't
  overloaded): `nba_scores` (team + natural `date` — today live, or "May 20th"/
  "yesterday"/"last Tuesday"/ISO), `nba_player` (live per-player points + line).
- Default-team logic: no team + no date → Nets ("what's the score"); dated + no
  team → all games that day ("what games were played on May 20th").
- Tests: `tests/test_unit_nba.py` (27 pure tests vs ESPN's real schema + a
  `WES_NBA_LIVE=1` schema-drift canary). Full suite 193 passed. Golden eval
  14/14 after adding the tools (no router regression).
- Verified live on voice + Discord: live "what's the score", per-player points,
  and dated "what games were played on May 20th" (Thunder def. Spurs 122-113)
  all answer from the tool with no hallucinated numbers.
- Injection note: P1 surfaces only structured ESPN numbers + team/player names
  (no free prose), so the injection surface is minimal; the explicit untrusted-
  content guard lands with P1b (reddit/news free text). Documented in-module.

## P1b — SHIPPED 2026-07-07
- `nba_discussion` tool → `wes_nba.subreddit_discussion()` (default r/GoNets).
  Reddit's JSON API 403s unauthenticated and OAuth needs an app; **RSS** serves
  fine to a browser UA with no key — chose RSS. Atom parse (stdlib), recent post
  titles + author + age.
- **Injection defense (the P1b security gate):** two layers — (1) the tool
  result is wrapped in an adjacent `[UNTRUSTED …]` guard stating it's quoted data
  to summarize, never instructions/facts, and no tool may be called from it;
  (2) `wes_server.WEB_CONTENT_RULE` appended to every channel's system prompt.
  Test `test_injection_content_stays_wrapped_as_data` asserts a hostile post
  title is surfaced only as quoted data under the guard.
- Reddit rate-limits rapid RSS repeats (429) → 5-min text cache (`_text_cache`);
  verified three rapid voice calls now all succeed.
- Tests: +7 in `tests/test_unit_nba.py` (RSS parse/format/guard/injection/cache
  + live-reachability canary). Verified live on voice + Discord — real r/GoNets
  posts summarized, grounded, no injection followed.
- Note: voice tool-calling on this is phrasing-sensitive (e4b sometimes offers
  instead of calling) — same class as #001/#028, not an NBA bug. Discord (12b)
  is reliable.

## Plan (refined 2026-07-07 — direct-first, free)
- **P1 (DONE)**: ESPN direct-REST tools (curated/wrapped so the small
  router isn't overloaded) → live score, today's games, live player box score;
  Nets as default team. Works voice + Discord. No key, no cost.
- **P1b (DONE)**: team-subreddit discussion via reddit RSS + injection guard.
  Generalized 2026-07-20 beyond the Nets: `nba_discussion` takes an optional
  `team` (name/nickname/city) resolved to that team's subreddit via a static
  30-team map (`team_subreddit`), defaulting to the Nets (r/GoNets). Works
  offseason (static map, unlike the games-dependent scores resolver).
- **P1b**: r/GoNets discussion tool (pick the reliable Reddit path — free
  read-only app or no-auth MCP; raw *.json often 403s from servers).
- **P2**: local nightly cache — **ALL 30 TEAMS, not just the Nets** (owner,
  2026-07-16): schedule/rosters/standings/box scores for instant offline
  reference; scheduled task in the nightly slot.
  **Storage is a non-issue** — a full season across all teams is ~20-50MB in
  sqlite (schedule ~250KB for 1,230 games; rosters ~250KB for ~500 players; box
  scores ~5MB raw, ~20MB indexed). Even a decade stays under a GB.
  **EXCLUDE play-by-play** (~1-2MB/game, ~1-2GB/season) — it buys fantasy
  nothing. The real constraint is **fetch volume vs ESPN's unofficial API**:
  backfill 1,230 games politely (rate-limited, resumable), then nightly deltas.
  This cache is also the ingestion pattern the #029 GM batch job reuses.
- **P3**: generalize the fetch/reddit tools onto an MCP client layer (reusable
  for future domains) + broader news (Yahoo RSS / Brave/Fetch MCP).
Direct-first because ESPN is a trivial REST API — MCP wrapping it adds
complexity for no gain; MCP earns its place at P3 for reddit/news/reuse.

### 2026-07-20 — general web search shipped (partially covers P3 "news")
The router now has a `search_web` handoff → Claude Haiku + server-side web search
(docs/pipeline.md), for any current/live fact the curated tools don't cover. This
delivers the ad-hoc "broader news / general internet" slice of P3 **without** an
MCP layer — so P3 narrows to the *reusable MCP client* + *curated* NBA feeds
(the ESPN/reddit tools stay purpose-built for reliability and the router's tool
budget; web search is the general fallback, not a replacement for them).
