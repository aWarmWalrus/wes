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
| `test` | the pytest suite (skips Pi hardware tests) |
| `eval [local\|haiku]` | reply-quality eval vs the golden set |
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
pc/      PC services: Flask server, Discord bot, fantasy engine
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

`Z:\` is still a Samba mount of the Pi's clone, useful for reading its files and
logs from the PC, but **nothing runs from it**. Previously the PC executed
`Z:\wes\pc\*.py`, which meant it couldn't start a service without the Pi (#032).
A Windows venv can't live on `Z:` either, so PC Python environments go on `C:`.

## Testing

```powershell
& C:\Users\awarm\wes-pc\wes-dev.ps1 test
```

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
| `docs/observability.md` | metrics, dashboards, alerts |

## A note on the fantasy manager

It writes to a **real Yahoo account**, and one team is configured to make
**irreversible add/drops unattended**. That is deliberate and confined to a
league whose outcome doesn't matter. Before touching `pc/wes_execute.py`, read
`docs/fantasy-gm-design.md` and ticket #029 — including the part where a write
targeted the wrong player, which is why every swap now targets by player name
and re-verifies against the live roster afterward.
