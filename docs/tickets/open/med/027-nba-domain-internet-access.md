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
- [ ] "what's the score of the Celtics game" → live/last data via tool, not guess
- [ ] "when do the Warriors play next" → from local cache, instant
- [ ] "what's r/nba saying about <topic>" → summarized real threads
- [ ] fetched web content cannot trigger memory writes/actions (injection test)
- [ ] curated tool surface — no router-quality regression in the eval
- [ ] nightly cache refresh runs as a scheduled task

## Notes
Phased: (P1) local cache + nightly updater + a couple of direct NBA tools to
prove value; (P2) MCP client layer generalizing it; (P3) reddit + news via MCP
with the injection guard. Ordering TBD by the decisions above.
