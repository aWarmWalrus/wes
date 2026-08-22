# WES — Walrus Embedded Assistant

A self-hosted, mostly-local voice assistant. Say "hey Jarvis" and it answers out
loud, sees what the camera sees, and runs a fantasy football team on its own.

Three tiers, so the latency-sensitive parts stay on the edge and the heavy parts
run on a GPU that isn't battery-powered:

| Tier | Host | Role |
|------|------|------|
| 1 | Raspberry Pi 5 + Hailo-8 | wake word, mic/VAD, speaker, on-device vision |
| 2 | Windows PC (RTX 5060 Ti 16GB) | Whisper STT, local LLM, piper TTS |
| 3 | Claude API | error fallback + deliberate escalation |

A turn: wake word → VAD-endpointed capture → Whisper on the PC → **gemma4:12b**
via Ollama (router, escalation and vision in one model) → piper TTS streamed
back sentence-by-sentence to a Bluetooth speaker. Claude is the fallback, not
the default. Full walkthrough: `docs/pipeline.md`.

There's also a Discord frontend (DM Jarvis, owner-allowlisted) and a Prometheus
+ Grafana dashboard.

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
| `test [file]` | the pytest suite, or just one file (skips Pi hardware tests) |
| `eval [local\|haiku]` | reply-quality eval vs the golden set |
| `draft-eval` | golden DRAFT scenarios with known right answers |
| `perf` | latency check against the recorded baseline |
| `health` | `GET /health` as JSON |
| `say <channel> <text>` | speak something |
| `turns` / `usage` | recent exchanges / token counters |
| `log [service] [n]` | tail a service log |
| `gpu` | `nvidia-smi` + `ollama ps` |
| `models [status\|check\|list\|load\|unload\|fit]` | VRAM/model manager |
| `deploy [check]` | redeploy launchers from the repo (`check` = drift report only) |

**Run `models check` after any model change.** It compares configured vs
actually-resident models. A silent mismatch there once sent every request to
Claude for a week while `/health` cheerfully echoed the intended config.

### The launcher scripts

Each background service is a Windows **Scheduled Task** that runs one launcher.
The launchers set environment (voice, model, feature flags) and then exec Python.

| Script | Scheduled task | What runs |
|---|---|---|
| `run_server.ps1` | `WES Server` | Flask API on `:8080` — STT, LLM, TTS, tools |
| `run_discord.ps1` | `WES Discord` | Discord bot + alert/fantasy watchers |
| `run_exporters.ps1` | `WES Exporters` | Prometheus exporters |
| `run_fantasy_gm.ps1` | `WES Fantasy GM` | the fantasy GM cycle (4 triggers/week) |
| `run_nightly_eval.ps1` | `WES Nightly Eval` | nightly quality eval |
| `run_sleeper_draft.ps1` | — (run by hand) | Sleeper draft day — pre-flight, wait, draft |
| `wes-dev.ps1` | — | the dev helper above |
| `wes-models.ps1` | — | VRAM/model manager (via `wes-dev models`) |
| `deploy.ps1` | — | copies all of the above from the repo to local disk |

> **Canonical copies live in `pc/scripts/` in this repo; the scheduled tasks run
> DEPLOYED copies from `C:\Users\awarm\wes-pc\`.** Edit the repo copy, then
> `wes-dev.ps1 deploy` — `deploy check` reports drift without changing anything.
>
> They must sit on local disk for two independent reasons: a launcher on `Z:`
> can't start before the share is mapped at boot (#032), and PowerShell's
> execution policy refuses to run an unsigned script off a network share at all.
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

**To run it on a mock, or on anyone else's draft:** join the draft first (the
seat is claimed on joining), then pass its id — the number in the URL
`sleeper.com/draft/nfl/<id>`:

```powershell
& C:\Users\awarm\wes-pc\run_sleeper_draft.ps1 -DraftId 1394956625890017280
```

The seat comes from that draft's own `draft_order`, since a mock has no league
at all. Scoring still comes from `-League` (your real one by default) because a
mock has no scoring of its own — so a mock values players exactly as the real
league would. `-RebuildSnapshot` refreshes the board first.

Run `-Check` a day ahead too. Every item in it is a failure that has already
happened once: a missing token stood down on all 15 picks while `cpu_autopick`
took them and printed a plausible roster; a dead Ollama silently turns every
pick into the engine's sort; disabled writes log `WOULD take` and never click.

Raw equivalents, only when `wes-dev.ps1` lacks a flag you need:

```powershell
Stop-ScheduledTask -TaskName "WES Server"; Start-ScheduledTask -TaskName "WES Server"
Get-Content C:\Users\awarm\wes-pc\logs\server.log -Tail 20
```

### Pi

```bash
ssh walrus-pi                          # alias -> walrus@10.0.0.79
systemctl --user restart wes-client    # after editing pi/wes_client.py
journalctl --user -u wes-client -n 30  # logs
```

The client is a **user** systemd unit (`pi/wes-client.service`), boot-persistent.
Hailo and OpenCV code (`hailo_*.py`) needs the Pi's **system** `python3`; the
wake-word and audio deps live in `~/wes/.venv`.

### After a reboot

Run **`docs/startup-checklist.md`**. A dead service here is silent — the logon
tasks can die before `Z:` is mapped and still report success (#032). Don't
assume a quiet system is a working one.

---

## Layout

```
pi/      Pi client: wake word, VAD, audio, Hailo vision (faces, objects)
pc/      PC services: Flask server, Discord bot, fantasy engine + drafting agent
tests/   pytest suite + eval harness + perf checks
docs/    subsystem docs and the ticket tracker
observability/  Prometheus + Grafana provisioning
hosts.yaml      THE source of truth for IPs and ports (read at runtime)
```

**Each machine has its own clone and runs from its own local disk:**

```
Pi   /home/walrus/claude/wes      runs pi/
PC   C:\Users\awarm\wes           runs pc/
GitHub                            the hub they sync through
```

So a change isn't live on the other machine until it's pushed and pulled —
`wes-dev.ps1 sync` does both and warns if the clones are on different commits.

**Edit the clone belonging to the machine that runs the code:**

| Changing | Edit in |
|---|---|
| `pc/**` — server, Discord, fantasy | `C:\Users\awarm\wes` |
| `pi/**` — voice client, Hailo vision | `Z:\wes` (that *is* the Pi's clone) |
| shared — `wes_hosts.py`, `hosts.yaml`, `tests/`, `docs/` | either, then `sync` |

> **The trap:** editing `Z:\wes\pc\wes_server.py` and reloading the server does
> **nothing** — that's the Pi's copy, which no PC service reads. It fails
> *silently*: the service restarts, reports healthy, and runs the old code. This
> was the correct workflow before 2026-08-08, so older notes point the wrong way.

`Z:\` is still a Samba mount of the Pi's clone, useful for reading its files and
logs from the PC, but **nothing runs from it**. Previously the PC executed
`Z:\wes\pc\*.py`, which meant it couldn't start a service without the Pi (#032).
A Windows venv can't live on `Z:` either, so PC Python environments go on `C:`.

## Testing

```powershell
& C:\Users\awarm\wes-pc\wes-dev.ps1 test                          # all 943, ~13s
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

CI (`.github/workflows/ci.yml`) runs the hardware- and API-free suite on every
push and PR. **Add a test in the same change as the feature** — and keep unit
tests free of hardware and network so they stay fast and CI-safe.

## Where to read next

| File | For |
|---|---|
| `CLAUDE.md` | architecture, invariants, hard-won gotchas — **start here** |
| `docs/startup-checklist.md` | is anything actually running? |
| `docs/pipeline.md` | the turn lifecycle end to end |
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
| `pc/wes_sleeper.py` | Sleeper adapter: reads via the JSON API, writes via Playwright |
| `pc/wes_draft.py` | pure draft maths — snake slots, value over replacement, roster fit |
| `pc/wes_draft_agent.py` | builds the shortlist, asks a local model to choose |
| `pc/sleeper_draft_run.py` | the loop: poll the clock, decide, submit, verify |
| `pc/sleeper_draft_day.py` | pre-flight, wait for the room, hand off |
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
