# Open tickets

The current WES work queue — **open tickets only** (closed ones live in
`done/`, out of context). One line each; open the file for full context.
Conventions + workflow: `README.md`. Next free id: **033** (open here;
001-003 + 014-024 shipped in `done/2026-07/`).

## High priority

- [032](open/high/032-services-die-at-boot-z-drive-race.md) — **Boot reliability bug**: after the 2026-07-28 reboot all three logon tasks died instantly (`Z:\` not mapped yet → `can't open file 'Z:\\wes\\pc\\wes_server.py'`), taking voice + Discord down ~24h **silently** — launcher exit code masked it (`LastTaskResult 0`) and the metrics-missing alert couldn't be delivered because the Discord bot was down too. Fix: UNC/wait-for-path in the launchers, propagate exit codes, and a watchdog that doesn't share fate with the services. Manual mitigation: `docs/startup-checklist.md`
- [029](open/high/029-fantasy-gm-autopilot.md) — **Fantasy GM epic**: Jarvis autonomously manages Yahoo NBA teams (read → value → optimize → gated execute), per-team autonomy. Official Yahoo API abandoned (approval + no-caching ToS) → **Playwright browser automation**. **P0+P1 shipped, P2 engine done (2026-07-21)** — `fantasy_my_team` + `fantasy_player_value` live; P2 optimizer (`optimize_lineup`, exact + property-tested, in `pc/wes_fantasy.py`) built but tool registration deferred to in-season. **P7 PULLED FORWARD 2026-07-29 — NFL first**, because NFL season starts ~Sept 10 vs NBA's late October, so the epic stops being blocked on the offseason: optimizer is now multi-sport (`_SPORTS` tables + `infer_sport`, NBA unchanged by default) and `pc/wes_nfl.py` adds the points-based valuer (also closes #030's NFL-valuer TODO). **Two real NFL leagues discovered 2026-07-29** (`pc/yahoo_league_discover.py`) and recorded in `~/wes-pc/teams.yaml` — they were invisible because `my_teams()` only scrapes `/nba/` links: `nfl.l.424494` "Teletubbies" (pre-draft, the league #030 was mock-drafting in) and `nfl.l.957011` "Charles's Pop" (the accidental public league, **already drafted** → designated throwaway = shadow-soak testbed with real data). **So the "wait for the draft" blocker is gone.** Remaining: sport-parameterize `wes_yahoo.py` (unblocked NOW — note NFL URLs are `/f1/`, not `/nfl/`), NFL player pool, weekly cadence, then wire the tool + shadow-soak through September. Roadmap in `docs/fantasy-gm-design.md`
- [028](open/high/028-planner-multistep-reasoning.md) — Planner/orchestrator for ambiguous + multi-step queries; sibling to #026. **Option A shipped 2026-07-21** — `nba_schedule` + `nba_top_performers` tools close the two named example queries; full ReAct/plan-execute (B/C) still open, blocked on #026's routing decision. Not on #029's critical path (design §8.1)

## Medium priority

- [030](open/med/030-fantasy-draft-tool.md) — Fantasy draft tool: **autonomous** end-to-end Yahoo draft agent, now **multi-sport, NFL-first** (owner joining a real NFL league this season; NBA second). Sport-agnostic draft-room automation + common recommender; **valuation sport-specific** (NBA roto z-scores DONE in `wes_draft`/`wes_fantasy`; NFL points-based valuer TODO). **NFL mock drafts are LIVE now** — entry path mapped (`livedraft_selection`→Draft Now→waiting-room; `mock_lobby` instant-mock); live-room DOM capture is an owner-driven `yahoo_draft_recon.py` run away. Then write the shared live-board scraper + pick-submit
- [004](open/med/004-smart-home-controls.md) — Smart home tools: Hue-direct first, Home Assistant later (feasibility confirmed)
- [005](open/med/005-scheduled-actions.md) — Scheduled actions: timers, reminders, recurring routines
- [012](open/med/012-durable-agentic-memory.md) — Unified durable memory: Phase 1 (MEMORY.md + remember/forget) shipped; remaining = nightly consolidation, temporal facts, per-person notes
- [026](open/med/026-adaptive-thinking-budget-router.md) — Adaptive thinking budget: **L1+L2 shipped 2026-07-21** — `effort` (standard/deep) arg on `escalate_hard` sizes the deep-tier budget; router proposes it, Discord path unchanged. Remaining: verify-and-escalate net (step 3) + unify Discord (step 4), coupled to #028
- [031](open/med/031-scraper-drift-self-repair.md) — Scraper drift: scheduled canary → Jarvis alert → **escalate repair to Claude** (ESPN JSON = Claude-patch-for-review; Yahoo DOM = Claude computer-use/human). Local 12b limited to detect+alert, NOT autonomous scraper rewrite (assessed too weak — context budget + multi-step + unattended-write risk)
- [027](open/med/027-nba-domain-internet-access.md) — NBA domain expertise: **P1 + P1b shipped** (ESPN live scores/player points/dated results + team-subreddit discussion via RSS with injection guard, any NBA team, both channels). Remaining: P2 nightly cache, P3 MCP/news (partly covered by general web search)

## Low priority / someday

- [006](open/low/006-observability-phase-3b.md) — Observability 3b: turn-latency histograms on /metrics + more alert rules
- [007](open/low/007-barge-in-interruption.md) — Barge-in: cut the reply off by speaking mid-playback
- [008](open/low/008-single-call-scene.md) — Inject prefetched scene into context to skip the vision tool round-trip (KR3)
- [009](open/low/009-multi-person-disambiguation.md) — Verify clothing-color disambiguation with 3+ people in frame (KR4)
- [010](open/low/010-future-hailo-use.md) — Use spare Hailo headroom: pose/gesture, ambient detection, wake-word on-device
- [011](open/low/011-eval-flakes.md) — Eval flakes: lexicon-names STT + math-simple arithmetic
- [013](open/low/013-storage-tier.md) — Storage tier on the PC for audio/video/transcripts
- [025](open/low/025-dashboard-gpu-temp-power-axis.md) — Dashboard "GPU temperature / power" panel mixes °C and W on one axis (bug)
