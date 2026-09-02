---
id: 032
title: Services die at boot when Z:\ isn't mapped yet (24h silent outage)
status: open
priority: high
created: 2026-07-29
closed:
tags: [ops, reliability, scheduled-tasks, observability]
related: [docs/startup-checklist.md, docs/setup.md, "#006"]
---

## Problem / Goal

The PC rebooted **2026-07-28 20:38:58**. The three logon-triggered scheduled
tasks fired 41s later at 20:39:39 and **all three failed instantly** because
`Z:\` (the Samba mount `\\10.0.0.79\claude` that holds the repo) had not been
remapped yet. Mapped network drives are restored asynchronously per-logon; the
tasks won the race and lost.

Evidence — identical in `logs/server.log`, `logs/discord.log`, `logs/eval_last.log`:

```
python.exe: can't open file 'Z:\\wes\\pc\\wes_server.py': [Errno 2] No such file or directory
  At C:\Users\awarm\wes-pc\run_server.ps1:47 char:1
```

Consequences, all silent:

- **WES Server** and **WES Discord** were down from 2026-07-28 20:39 until
  manually started 2026-07-29 (~24h). Voice loop and Discord frontend dead.
- The task's 3× auto-restart didn't help — the failure is deterministic, not transient.
- Task `LastTaskResult` was **`0`** for both. The launcher's exit code is
  PowerShell's, not the child python's, so Task Scheduler recorded success.
- The **nightly eval** ran on schedule 2026-07-29 03:30, failed the same way,
  and left no row in `eval_history.csv` / `perf_history_stream.csv` (last rows:
  2026-07-27). The metrics gap was the only visible symptom.
- **No alert fired.** The Grafana/Prometheus `wes_server` metrics-missing rule
  did fire, but its delivery path is the Discord bot — which was also dead. The
  DM landed only *after* the bot was restarted by hand. **Alerting cannot
  report an outage that takes down the alerter.**

## Approach

Three independent fixes; the first is the actual bug, the others stop it from
being silent next time.

1. **Don't depend on the drive letter.** Point the launchers at the UNC path
   (`\\10.0.0.79\claude\wes\...`) instead of `Z:\`. UNC needs no per-logon
   mapping. Alternative/additional: a wait-for-path preamble in each launcher
   (poll up to ~120s for the repo path, then run) — worth having anyway, since
   a Pi that boots slower than the PC breaks UNC too.
   - Note `net use Z:` resolves to `\\10.0.0.79\claude` — a hardcoded IP, while
     `hosts.yaml` moved the PC to mDNS on 2026-07-24. Consider
     `\\raspberrypi\claude` for symmetry, but the Pi's address is the stable one.
2. **Make the launchers fail loudly.** Propagate the child's exit code
   (`exit $LASTEXITCODE`) so `LastTaskResult` is non-zero, and have the launcher
   detect a missing repo path and write an unmistakable first log line.
3. **Watchdog that doesn't share fate with the services.** Cheapest version: a
   scheduled task every N minutes that probes `/health` and, if it fails,
   Start-ScheduledTask's the server — no Discord dependency. Pairs with #006
   (more alert rules). Out-of-band notification (something not on the PC) is the
   stronger fix but a bigger change.

## Acceptance

- [ ] Launchers start correctly after a cold reboot with `Z:\` unmapped
      (verify by unmapping `Z:` and running the task, not just by rebooting).
- [ ] A launcher whose script path is missing leaves `LastTaskResult != 0`.
- [ ] A server that is down for >N minutes gets restarted or reported without
      the Discord bot being alive.
- [ ] `docs/setup.md` records the UNC/wait-for-path requirement for any new
      PC-local launcher.

## Notes

- 2026-07-29: found during a repo status review; services restored by hand
  (`wes-dev.ps1 reload` + `reload discord`), `/health` ok, `models check` clean,
  332 unit tests pass, `perf_check` within thresholds (ttfa 2197ms vs 1918
  baseline — cold-loaded model, not a regression).
- The gemma4:12b pin is fine and needs no separate fix: the server's `warmup()`
  sets `keep_alive=-1`, so "nothing resident" was a *symptom* of the dead
  server, not a pinning bug.
- Wrote `docs/startup-checklist.md` in the same pass — the manual mitigation
  until this ticket lands.
- This is the third bug in this family (see the `wes-reload` skill's
  "smoke-test via the scheduled task" section: the un-hidden Exporters console
  and the cp1252 emoji death). The pattern: **the scheduled-task environment is
  not your shell**, and here specifically it doesn't have your drive letters.

## Update 2026-09-02 — the root cause is gone; two of three fixes still matter

The Raspberry Pi was repurposed and the `Z:\` share went with it. Both clones
are gone: the repo lives on the PC's local disk at `C:\Users\awarm\wes`, the
launchers are deployed to `C:\Users\awarm\wes-pc\`, and nothing a scheduled task
touches lives on a network path. **Fix 1 (don't depend on the drive letter) is
therefore moot** — there is no drive letter and no host that has to be awake
first.

What is NOT fixed, and is the reason this stays open:

2. **The launchers still swallow the child's exit code.** `LastTaskResult` was
   `0` for a process that never started, and it still would be. A different
   cause — a broken venv after the E: migration, a syntax error in a launcher —
   produces exactly the same silent success.
3. **There is still no watchdog that outlives the services.** This is the
   sharper half. The August 2026 incident (`docs/observability.md`) proved the
   point independently of any drive letter: the server and the Discord bot died
   on the same afternoon and nobody noticed for a week, because the alert
   watcher lives *inside* the Discord bot. Moving Prometheus onto this PC does
   not help — it will happily fire `TargetDown` at a bot that is not running to
   receive it.

So this ticket is now really "make a dead service loud", and the Z: race was one
instance of it. Retitle if it is ever picked up.
