# WES — Walrus Embedded Assistant

A self-hosted, mostly-local AI assistant that drafts and manages real fantasy
football teams on its own. DM it on Discord and it answers; the rest of the time
it runs its own scheduled cycles.

Two tiers, so the reasoning is free by default and only pays when it needs to:

| Tier | Host | Role |
|------|------|------|
| 1 | Windows PC (RTX 5060 Ti 16GB) | **gemma4:12b** via Ollama — router, tools, and the thinking tier |
| 2 | Claude API | error fallback, live web search, deliberate escalation |

A turn: your message → the local model, which either answers, calls a tool
(fantasy, NBA, date/time, memory), or escalates itself to the thinking tier →
reply. Claude is the fallback, not the default.

There is also a Prometheus + Grafana dashboard, in Docker on the same PC.

> **It used to be a voice assistant.** Until 2026-09-02, tier 1 was a Raspberry
> Pi 5 + Hailo-8 that did wake word, mic capture with VAD endpointing, Bluetooth
> playback and on-device vision, and the PC ran Whisper STT and piper TTS around
> the model. That hardware was repurposed, so all of it — STT, TTS, the camera
> and face-recognition tools, and the audio endpoints — came out.
> **`archive/pi/README.md` is the map**; the design write-ups are in
> `docs/archive/`.

> **`CLAUDE.md` is the working doc** — architecture detail, invariants, and the
> traps that have actually bitten. Read it before changing anything. This file
> is the orientation layer.

---

## Managing the machines

### PC — one helper script for everything

All PC operations go through **`wes-dev.ps1`**. Prefer it over raw commands: it
is allowlisted, so it runs without a permission prompt, and it waits for
`/health` after a restart instead of returning before the service is up.

```powershell
& C:\Users\awarm\wes-pc\wes-dev.ps1 <command>
```

| Command | Does |
|---|---|
| `reload [server\|discord\|exporters]` | restart the scheduled task, wait for `/health` |
| `test [file]` | the pytest suite, or just one file |
| `eval [local\|haiku]` | reply-quality eval vs the golden set |
| `draft-eval` | golden DRAFT scenarios with known right answers |
| `perf` | latency check against the recorded baseline |
| `health` | `GET /health` as JSON |
| `say <channel> <text>` | one text turn through `/respond_text` |
| `turns` / `usage` | recent exchanges / token counters |
| `log [service] [n]` | tail a service log |
| `gpu` | `nvidia-smi` + `ollama ps` |
| `models [status\|check\|list\|load\|unload\|fit]` | VRAM/model manager |
| `deploy [check]` | redeploy launchers from the repo (`check` = drift report only) |
| `obs [ps\|"up -d"\|logs]` | the Prometheus/Grafana docker stack |

**Run `models check` after any model change.** It compares configured vs
actually-resident models. A silent mismatch there once sent every request to
Claude for a week while `/health` cheerfully echoed the intended config.

### The launcher scripts

Each background service is a Windows **Scheduled Task** that runs one launcher.
The launchers set environment (model, feature flags) and then exec Python.

| Script | Scheduled task | What runs |
|---|---|---|
| `run_server.ps1` | `WES Server` | Flask API on `:8080` — the model, the tool loop, memory |
| `run_discord.ps1` | `WES Discord` | Discord bot + alert/fantasy watchers |
| `run_exporters.ps1` | `WES Exporters` | Prometheus exporters |
| `run_fantasy_gm.ps1` | `WES Fantasy GM` | the fantasy GM cycle (4 triggers/week) |
| `run_nightly_eval.ps1` | `WES Nightly Eval` | nightly quality eval |
| `run_snapshot.ps1` | `WES Snapshot` | daily rebuild of the local fantasy board (05:40) |
| `run_sleeper_draft.ps1` | — (run by hand) | Sleeper draft day — pre-flight, wait, draft |
| `wes-dev.ps1` | — | the dev helper above |
| `wes-models.ps1` | — | VRAM/model manager (via `wes-dev models`) |
| `deploy.ps1` | — | copies all of the above from the repo to local disk |

> **Canonical copies live in `pc/scripts/` in this repo; the scheduled tasks run
> DEPLOYED copies from `C:\Users\awarm\wes-pc\`.** Edit the repo copy, then
> `wes-dev.ps1 deploy` — `deploy check` reports drift without changing anything.
>
> They sit on local disk. That was once mandatory for two independent reasons —
> a launcher on the Pi's `Z:` share couldn't start before the share was mapped at
> boot (#032), and PowerShell's execution policy refuses to run an unsigned
> script off a network share at all. Neither hazard survives the Pi, but keeping
> the deployed copy still means a `git pull` cannot rewrite a launcher mid-start.
> No secrets are in them — `ANTHROPIC_API_KEY`, `WES_DISCORD_TOKEN` and friends
> come from the user environment at launch.

### Draft day

The Sleeper draft is one event on one afternoon, so it is **not** a scheduled
task — a timer would either idle for weeks or start unattended. Run it by hand:

```powershell
& C:\Users\awarm\wes-pc\run_sleeper_draft.ps1 -Check   # pre-flight only
& C:\Users\awarm\wes-pc\run_sleeper_draft.ps1          # wait for the room, then draft
```

Start it any time before the scheduled draft time and leave it. It resolves the
draft and roster ids from the league (nothing is hard-coded), runs the
pre-flight, waits for Sleeper to open the room, and hands off to the pick loop.
It does **not** start the draft — that is Sleeper's on the commissioner's
schedule. Output is tee'd to `logs\sleeper_draft.log`.

It runs **headless** — no browser window steals focus while you work. Set
`WES_SLEEPER_HEADLESS=0` to watch it, which is worth doing if the draft room's
DOM has changed under us.

**Which Sleeper account** — `-User <name>`, defaulting to `gmbartimusprime`
(the bot). The token follows the name: `WES_SLEEPER_TOKEN_<NAME>` first, then
the shared `WES_SLEEPER_TOKEN`, so adding an account never displaces another.

```powershell
& C:\Users\awarm\wes-pc\run_sleeper_draft.ps1 -User awarmwalrus   # the real team
```

Naming an account switches its **credentials too**, not just the seat — those
used to move independently, so `-User` on its own drafted with the previous
account's token and said nothing. A name Sleeper does not know is refused
before anything writes, because the shared-token fallback means a typo would
otherwise succeed as somebody else.

**To run it on a mock, or on anyone else's draft:** pass its id — the number in
the URL `sleeper.com/draft/nfl/<id>` — and `-Join` to claim a free seat:

```powershell
& C:\Users\awarm\wes-pc\run_sleeper_draft.ps1 -Join -DraftId 1394956625890017280
```

`-Join` is opt-in because it is a **write** everyone in the room can see. Pair
it with `-Check` to get seated and verified without then drafting. It is
idempotent — re-running returns the seat already held rather than claiming a
second one.

The seat comes from that draft's own `draft_order`, since a mock has no league
at all. Scoring still comes from `-League` (your real one by default) because a
mock has no scoring of its own — so a mock values players exactly as the real
league would. `-RebuildSnapshot` refreshes the board first.

**Chat.** `-Banter` defaults to `auto`, which **posts to the room** under
whichever account is drafting. `-Banter propose` composes and logs without
sending — the right mode for a room of strangers; `off` is silent. Volume is
bounded in code, not by asking the model nicely: 60s between messages, 30s when
somebody names the bot, and a line making a checkable false claim (a pick
number that contradicts the board, a player it did not draft) is dropped before
it is sent and logged with the reason.

Every model call either agent makes — the payload in full and the raw reply —
lands in `GET /draft_turns` and the dashboard's **Draft agent** table.

Run `-Check` a day ahead too. Every item in it is a failure that has already
happened once: a missing token stood down on all 15 picks while `cpu_autopick`
took them and printed a plausible roster; a dead Ollama silently turns every
pick into the engine's sort; disabled writes log `WOULD take` and never click.

Raw equivalents, only when `wes-dev.ps1` lacks a flag you need:

```powershell
Stop-ScheduledTask -TaskName "WES Server"; Start-ScheduledTask -TaskName "WES Server"
Get-Content C:\Users\awarm\wes-pc\logs\server.log -Tail 20
```

### Monitoring

Prometheus and Grafana run in Docker, which on this machine lives inside WSL and
needs `sudo` — so `wes-dev.ps1 obs` wraps it:

```powershell
& C:\Users\awarm\wes-pc\wes-dev.ps1 obs ps
& C:\Users\awarm\wes-pc\wes-dev.ps1 obs "up -d"
```

Dashboard at <http://localhost:3001> (3001, not 3000 — Open WebUI has 3000).
The fragile part is that the containers reach Windows services across the WSL
NAT gateway, whose address changes when WSL restarts; see the `windows-host`
note in `observability/docker-compose.yml`.

### After a reboot

Run **`docs/startup-checklist.md`**. A dead service here is silent: the launcher
reports success for a Python process that never started (#032), and the alert
watcher lives inside the Discord bot, so it cannot report its own absence. Don't
assume a quiet system is a working one.

---

## Layout

```
pc/      the services: Flask server, Discord bot, fantasy engine + drafting agent
tests/   pytest suite + eval harness + perf checks
docs/    subsystem docs and the ticket tracker
observability/  the whole Prometheus + Grafana stack, as code
archive/ retired code, kept for reference — archive/pi/ is the voice tier
hosts.yaml      THE source of truth for the address and ports (read at runtime)
```

**One clone, on local disk: `C:\Users\awarm\wes`.** Edit there.

There were two until 2026-09-02 — the Pi ran `pi/**` from its own checkout,
reached over the `Z:` Samba share — and editing the wrong one failed *silently*:
the service restarted, reported healthy, and ran the old code. Both the second
clone and the share went with the hardware, so `wes-dev.ps1 sync` is now just
pull-and-redeploy. Older notes that say to edit `Z:\wes` are stale.

## Testing

```powershell
& C:\Users\awarm\wes-pc\wes-dev.ps1 test                          # all 1188, ~19s
& C:\Users\awarm\wes-pc\wes-dev.ps1 test test_unit_sleeper.py     # one file, ~2s
& C:\Users\awarm\wes-pc\wes-dev.ps1 test -k draft                 # by name
```

**One test file per module, and none of them touch the network** — so a targeted
run is cheap and there is rarely a reason to sit through the whole suite while
iterating. A leading path runs only that file; a bare filename resolves against
`tests\`. Plain flags still apply to everything.

Nearly all of a targeted run is pytest starting up (~2s fixed); the slowest
single test is 2s and the rest average 14ms, so splitting the suite up further
would buy almost nothing. Run the whole thing before committing.

CI (`.github/workflows/ci.yml`) runs the API-free suite on every push and PR.
**Add a test in the same change as the feature** — and keep unit tests free of
network and hardware so they stay fast and CI-safe.

## Where to read next

| File | For |
|---|---|
| `CLAUDE.md` | architecture, invariants, hard-won gotchas — **start here** |
| `docs/startup-checklist.md` | is anything actually running? |
| `docs/archive/` | the retired voice tier: pipeline, audio, vision, hardware |
| `docs/setup.md` | building the environments from scratch |
| `docs/tickets/INDEX.md` | the current work queue |
| `docs/data-architecture.md` | layer boundaries — read before touching data code |
| `docs/fantasy-gm-design.md` | the autonomous fantasy manager |
| `docs/tickets/open/high/039-sleeper-platform.md` | Sleeper + the drafting agent, with every wrong turn |
| `docs/observability.md` | metrics, dashboards, alerts |

## The drafting agent

WES drafts a real Sleeper team autonomously — no human on the clock (#039). It
is **agentic**, not a ranked list:

| Module | Does |
|---|---|
| `pc/sleeper/data.py` | Sleeper adapter: reads via the JSON API, writes via Playwright |
| `pc/wes_draft.py` | pure draft maths — snake slots, value over replacement, roster fit |
| `pc/wes_draft_agent.py` | builds the shortlist, asks a local model to choose |
| `pc/sleeper/draft_run.py` | the loop: poll the clock, decide, submit, verify |
| `pc/sleeper/draft_day.py` | pre-flight, wait for the room, hand off |
| `pc/wes_snapshot.py` | the local board (players, projections, byes, crosswalk) |
| `tests/draft_replay.py` | replay a finished draft, compare pick-makers offline |

**The safety property is that the engine constrains the choice set and the model
chooses within it.** Every candidate is verified available by id, legal under
the same-team cap, and actually valued — so a hallucinated name cannot become a
pick, and anything off the shortlist falls back to the engine's own top choice.
This differs from the judgment gate in #038, which may only *subtract*: a draft
pick is **mandatory**, so "do nothing" isn't available and a veto-only model
cannot draft at all.

Two things worth knowing before changing any of it:

- **A cache is not a verification.** Availability is re-checked with `ttl=0`
  immediately before a pick, and `submit_pick` polls uncached to confirm its own
  click. Re-checking through the same 15s cache the board was built from cost a
  pick and looked exactly like a race.
- **An empty list is not an absent player.** Sleeper renders ~59 rows, ordered
  by its own ranking, so "not in the list" can mean windowed, unpainted, or
  genuinely taken. These now raise *different* errors, because only one of them
  means pick someone else.

## A note on the fantasy manager

It writes to a **real Yahoo account**, and one team is configured to make
**irreversible add/drops unattended**. That is deliberate and confined to a
league whose outcome doesn't matter. Before touching `pc/wes_execute.py`, read
`docs/fantasy-gm-design.md` and ticket #029 — including the part where a write
targeted the wrong player, which is why every swap now targets by player name
and re-verifies against the live roster afterward.

The same applies to Sleeper: the draft loop makes **real, irreversible picks in
a real league** with nobody watching. `cpu_autopick` is the fallback and it is a
good one, so every failure path stands down rather than retrying into the clock
— a missed pick costs a little value, a double pick costs a roster spot.
