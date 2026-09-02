# WES — Walrus Embedded Assistant

A local AI assistant on **one PC** (RTX 5060 Ti 16GB), with the **Claude API** as
its fallback and web-search tier. Local **gemma4:12b** via Ollama
(`WES_LLM=local`; streaming + tools) is both router and escalation (thinking)
tier. It is reached by Discord DM, and it manages the owner's fantasy teams on a
schedule. Turns keep a per-channel sliding-window conversation memory plus
durable facts in MEMORY.md.

> **WAS A 3-TIER VOICE ASSISTANT until 2026-09-02.** Tier 1 was a Pi 5 + Hailo-8
> doing wake word, mic capture, Bluetooth playback and on-device vision; the PC
> did Whisper STT and piper TTS around the model. The owner repurposed the Pi, so
> STT, TTS, the camera/face tools, the speculative-prefetch cache and the
> `/respond`, `/respond_stream`, `/speculate` and `/prefetch_scene` endpoints
> were all removed — nothing produced audio into them or consumed audio out of
> them. **`archive/pi/README.md` is the map of what went where**; `docs/archive/`
> holds the design write-ups (pipeline, audio, vision, hardware). Read those
> before adding any voice or vision feature back.

> **Model topology (2026-07-16): ONE model.** Was e4b-router + 12b-escalate; the
> e4b tag vanished from Ollama and every router call silently fell back to Claude
> for a week (`/health` echoes config, not reality). Collapsed to 12b alone as the
> project pivoted to batch/analysis. Measured: 7.0GB weights, **7.8GB resident at
> `num_ctx=16384`** (~50MB KV per 1k ctx), 6.3GB free. **Run `wes-dev.ps1 models
> check` after any model change** — it catches exactly this drift.

> This root file is loaded every session — keep it lean. Put subsystem detail in
> `docs/*.md` and read those on demand. (`@import` does NOT save context; a pointer I
> choose to follow does.)

## Machine & layout

| Host | LAN address | Role |
|------|--------|------|
| `DESKTOP-R2PFF9T` | DESKTOP-R2PFF9T.local | PC (RTX 5060 Ti 16GB) — all of WES |

> **`hosts.yaml` (repo root) is the single source of truth** for the address and
> service ports — read at runtime via `wes_hosts.py` by the server and the bot
> (env vars still override). Jarvis reads it through the `lookup_hosts` tool.
> Edit there when the network changes; this table is a convenience mirror.

- **ONE CLONE, on local disk: `C:\Users\awarm\wes`.** Edit here, full stop.
  There used to be two — the Pi ran `pi/**` from its own checkout, reached over
  the `Z:` Samba share, and editing the wrong one failed *silently* (the service
  restarted fine and ran the old code). That is over; `Z:` goes away with the Pi,
  and `wes-dev.ps1 sync` now just pulls and redeploys the launchers.
  **Any note or habit that says to edit `Z:\wes` is stale.**
- **Ports on this machine are crowded and have bitten twice.** 8080 WES server,
  3000 Open WebUI, 3001 Grafana, 9090 Prometheus, 11434 Ollama. A WSL service
  binding `127.0.0.1:<port>` beats a Windows `0.0.0.0` bind for IPv4 loopback,
  which is how Open WebUI silently answered as the WES server for a while. When
  something healthy is serving the wrong thing, check
  `netstat -ano | findstr LISTENING`.
- Python: the runtime venv is `C:\Users\awarm\wes-pc\.venv` (built from the
  `E:\miniconda3` base). Ollama models live in `E:\.ollama\models`, set in the
  Ollama app's own `db.sqlite`, which **overrides** `OLLAMA_MODELS`.

## Access & run

```powershell
# PC ops go through ONE allowlisted helper (no per-command prompt):
& C:\Users\awarm\wes-pc\wes-dev.ps1 <cmd>
#   reload [server|discord|exporters]  restart the task + wait for /health
#   test | eval [local|haiku] | perf   run the suites (see wes-test skill)
#   say <channel> <text> | reset | turns | usage | health | log [svc] [n]
#   gpu                                nvidia-smi + ollama ps
#   models [status|check|list|load|unload|fit]   VRAM/model manager (pin, drift-check)
#   obs [ps|"up -d"|logs|"restart grafana"]      the Prometheus/Grafana docker stack
# Raw equivalents (only if a flag the helper lacks is needed):
Stop-ScheduledTask -TaskName "WES Server"; Start-ScheduledTask -TaskName "WES Server"
Get-Content C:\Users\awarm\wes-pc\logs\server.log -Tail 20   # server log
```
> **Managing what's in VRAM:** `wes-dev.ps1 models status` shows resident/pinned
> models + config-vs-reality drift; `models load <tag>` pins a model
> (`keep_alive=-1`), `models unload` frees it, `models fit` checks headroom.
> Run `models check` after ANY model change — it catches the silent
> config/reality drift that once fell back to Claude for a week.

## Key files

- `pc/wes_server.py` — Flask `:8080`: the router + tool loop, escalation,
  conversation and durable memory, and the fantasy tools. Text in
  (`POST /respond_text`), text out.
- `pc/wes_discord.py` — the Discord frontend, and the **only** interactive way
  in: owner-allowlisted DMs → `POST /respond_text` (its own conversation
  channel). Runs as scheduled task "WES Discord" (`WES_DISCORD_TOKEN` +
  `_OWNER_ID` from PC user env; see `docs/setup.md`). Also runs two background
  watchers in the same process: `alert_watch` (Prometheus alerts) and
  `fantasy_watch` (#029 — DMs the owner when a real fantasy write happens,
  polling `wes_execute`'s ledger). Both phrase via `/announce`.
- `hosts.yaml` + `wes_hosts.py` (repo root) — the host registry (address/ports)
  and its loader; imported by the server and the bot. Jarvis reaches it via the
  `lookup_hosts` tool.
- `observability/` — the monitoring stack as code: `docker-compose.yml`
  (Prometheus + Grafana, on this PC since the Pi went), scrape config, alert
  rules, Grafana provisioning, dashboard JSON. On the Pi, half of this lived
  unversioned in `/etc`.
- `pc/scripts/*.ps1` — the launchers + `wes-dev.ps1`, **in the repo** (canonical).
  The scheduled tasks run **deployed** copies from `C:\Users\awarm\wes-pc\`: edit
  the repo copy, then `wes-dev.ps1 deploy` (`deploy check` reports drift).
- `tests/` — the test suite.
- `archive/pi/` — the retired tier-1 code (wake word, Hailo vision, the `:8090`
  state endpoint, the cast utility), kept as reference. Not imported, not
  tested, not deployed.

Project skills in `.claude/skills/`: **wes-test** (running the suites
correctly) and **wes-reload** (restarting the live services + reading logs) —
prefer them over re-deriving commands.

## Testing (do this every change)

Suite in `tests/` (full guide: `tests/README.md`). `$py = C:\Users\awarm\wes-pc\.venv\Scripts\python.exe`.
- Changed `pc/wes_server.py` → `& $py -m pytest C:\Users\awarm\wes\tests\test_unit_server.py -q`
- Anything latency-affecting → `& $py C:\Users\awarm\wes\tests\perf_check.py` (flags regressions vs
  baseline; records to `tests/perf_history_text.csv`)
- Anything reply-quality-affecting (prompts, routing, models) →
  `& $py C:\Users\awarm\wes\tests\eval_turns.py` (golden set vs live server + LLM judge —
  Haiku by default, `--judge local` for free gemma4:12b judging, `--judge
  both` to check their agreement; flags named-case regressions and
  judge-score drops; records to `tests/eval_history.csv`)
- E2E (real pipeline, costs a Claude call) → `& $py -m pytest C:\Users\awarm\wes\tests\test_e2e.py --run-e2e -q`
- **Add a test in the same change when you add a feature.** Keep unit tests
  hardware/API-free so they stay fast; reserve e2e for the full pipeline.
- **CI** (`.github/workflows/ci.yml`) runs the API-free suite on every push/PR
  (ubuntu, py3.11+3.12) from `requirements-dev.txt`, collecting `tests/` by
  directory minus `test_e2e.py`. A new unit test that needs an API key will fail
  there.

## Detailed docs (read on demand)

- `docs/data-architecture.md` — **read before touching the data path.** Layer
  boundaries (raw → fantasy data → regression → decision → model), the contracts
  between them, and the rule that imports point downward only. `pc/wes_http.py`
  is the one HTTP client; no module hand-rolls a fetcher (a test enforces it).
  Migration status + the remaining steps live in ticket #034.
- `docs/startup-checklist.md` — **run at the top of a session / after any
  reboot**: are the services actually running, is the model pinned, are the
  nightly metrics fresh, did anything die at boot. Exists because a dead service
  is silent here (see ticket #032).
- `docs/setup.md` — PC venv + dependency pins + auto-start tasks.
- `docs/tickets/` — the task tracker (one file per ticket). **`INDEX.md` lists
  open work by priority — read it for the current queue; don't load `done/`
  unless you need a shipped feature's history.** `docs/roadmap.md` is now just a
  short overview + project history that points here.
- `docs/observability.md` — Prometheus + Grafana, in Docker on this PC since the
  Pi went (<http://localhost:3001>): the PC exporters + `wes_server` `/metrics`
  (token counters) + `/turns` (recent-exchange log, size-capped `turns.jsonl`),
  alerting via the Discord bot, dashboard provisioning, restart commands.
- `docs/eval-design.md` — design for the automated quality-eval harness (golden
  set + LLM-as-judge + nightly gate); phases 1-2 (`tests/eval_turns.py`,
  deterministic checks + Haiku judge) are built.
- `docs/memory-design.md` — long-term memory design (OpenClaw-style file-based
  MEMORY.md + remember/forget tools + nightly consolidation); exploration only,
  not yet built.
- `docs/archive/` — **retired with the Pi**, kept for the record and for anyone
  reviving that tier: `pipeline.md` (the voice turn lifecycle, VAD endpointing,
  speculative prefetch), `audio.md` (JBL/Bluetooth, the A2DP persistent-silence
  fix, Google Cast), `vision.md` (Hailo `look`, `describe_scene`, face
  recognition + clothing-colour disambiguation), `hardware.md` (Pi/Hailo/camera
  specs).
- `docs/fantasy-gm-design.md` — Fantasy GM epic (#029): autonomous Yahoo
  fantasy team management (read → value → optimize → gated execute), per-team
  autonomy config + rails. **P0-P4 all shipped 2026-07-30**, live and
  writing for real. NFL-first (P7 pulled forward — season ~Sept 10 vs NBA's
  late Oct; `_SPORTS`/`_SITES` tables make the engine multi-sport, NBA
  unchanged by default). `pc/wes_yahoo.py` reads real rosters + league scoring
  settings; `pc/wes_nfl.py` values players from a paginated ESPN pool;
  `pc/wes_fantasy.py` optimizes the lineup; `pc/wes_execute.py` (P3) diffs
  against the real roster and **writes it to Yahoo for real** — gated by
  **per-ACTION** `autonomy` (`wes_execute.autonomy_for`; a scalar still means
  all actions) plus `actions_allowed` in `teams.yaml`, and the
  `WES_YAHOO_LIVE_WRITES` kill switch (**ON**, surfaced at `GET /health` as
  `fantasy_live_writes`). Every write re-verifies the roster afterward and
  targets swaps by player, never by slot type (a real mistake during
  development taught that lesson directly — see the ticket). Scheduled task
  **"WES Fantasy GM"** (P4; Windows Task Scheduler, NOT #005 — that's a
  separate, still-unbuilt general scheduler) runs it daily 6am PT + Sunday
  9:15am PT pre-lock, log-only. Every write is explained
  (`wes_execute.summarize_moves` — value comparison, or availability when
  that's the real driver) and DMed to the owner via `wes_discord.fantasy_watch`
  (a sibling of the alert watcher — polls the ledger, phrases via Jarvis).
  **Feature-complete for its original scope as of 2026-07-30**, live on one
  real team (`nfl.l.957011.t.4`, the owner's deliberately-chosen "don't care
  about the outcome" league). Roster management (#035) drops/adds too, and that
  team runs `add_drop: auto` — **it makes irreversible drops unattended on the
  scheduled run**, bounded only by `max_moves_per_week: 3`. Anywhere else a drop
  needs `approve={"drop","add"}` naming both players, re-checked against the
  live recommendation and refused if stale (never substituted). Open, named
  extensions: **real waiver claims** (it can only pick up true free agents, so
  `max_faab_bid_pct` is still enforced by no code), **no matchup/projection
  adjustment** (every value is a season aggregate), and it recommends the IL but
  never moves anyone there. Draft automation (#030) deprioritized — owner will
  draft manually. Full history + every finding: ticket #029.
  Platform pivoted from the official Yahoo API to browser automation (API access
  now requires a no-caching DocuSign).
- `docs/tickets/open/low/030-fantasy-draft-tool.md` — Fantasy DRAFT epic (#030):
  autonomous end-to-end Yahoo draft agent for AI-agent-run leagues. **Decision
  engine done 2026-07-21** — real per-league z-scores (`wes_fantasy.rank_by_zscore`,
  the population fetch #029 deferred, via ESPN's `byathlete` bulk endpoint) +
  `best_available` positional-need recommender in new `pc/wes_draft.py`,
  live-verified. Remaining: the live Yahoo draft-room automation (read the board,
  submit picks on the clock) — needs a recon session vs a real draft-room DOM.
- `docs/keyresults.md` — current-cycle key results, refreshed periodically.
