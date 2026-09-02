# Session startup checklist

Run this at the top of a working session (and after any reboot or power event)
**before** trusting anything about the system's state. It exists because a dead
service here is silent: nothing supervises the scheduled tasks, so one can be
down for a day while `/health` is never asked, the nightly eval quietly no-ops,
and nothing tells you. That happened on 2026-07-28 (#032) and again for a week
in August 2026 (`docs/observability.md`).

> **One host now.** This used to open by saying WES was three hosts with no
> supervisor above them. The Raspberry Pi was repurposed on 2026-09-02
> (`archive/pi/README.md`), so everything below runs on the PC. That removed a
> whole class of failure — the `Z:\` boot race, the two clones drifting, the Pi
> being asleep — and removed none of the silence.

Everything here is read-only except step 6.

## 1. Repo state

```powershell
git -C C:\Users\awarm\wes status --short
git -C C:\Users\awarm\wes log --oneline -5
```
- Uncommitted work is normal mid-feature, but know what it is before you build
  on it. `tests/eval_history.csv` + `tests/perf_history_text.csv` churn on their
  own — the nightly appends to them.

Then check the deployed launchers are in step with the repo:

```powershell
& C:\Users\awarm\wes-pc\wes-dev.ps1 sync            # pull + redeploy
& C:\Users\awarm\wes-pc\wes-dev.ps1 deploy check    # drift only, changes nothing
```
- The `.ps1` files are canonical in `pc/scripts/` and **deployed** to
  `C:\Users\awarm\wes-pc\`. Drift means someone edited a deployed copy, or a
  repo change was never pushed out — either way the running system isn't the
  one in git. `deploy` (no `check`) fixes it; a redeployed launcher only takes
  effect on the **next task restart**.

## 2. Are the services actually up?

```powershell
Get-ScheduledTask -TaskName 'WES Server','WES Discord','WES Exporters','WES Nightly Eval' |
  Select-Object TaskName,State
```
- **`State` must be `Running` for Server / Discord / Exporters.** `Ready` means
  *not running* — this is the failure that hides. "Nightly Eval" is correctly
  `Ready` between its 03:30 runs.
- Then the real probe:
  ```powershell
  & C:\Users\awarm\wes-pc\wes-dev.ps1 health     # ok:true + the llm topology line
  ```
- And the monitoring stack, which is Docker-inside-WSL:
  ```powershell
  & C:\Users\awarm\wes-pc\wes-dev.ps1 obs ps     # both containers Up
  ```

## 3. Did anything crash at boot?

```powershell
(Get-CimInstance Win32_OperatingSystem).LastBootUpTime
Get-ScheduledTaskInfo -TaskName 'WES Server' | Select-Object LastRunTime,LastTaskResult
Get-Content C:\Users\awarm\wes-pc\logs\server.log  -Tail 15
Get-Content C:\Users\awarm\wes-pc\logs\discord.log -Tail 15
```
- Task-redirected logs are UTF-16-ish; strip nulls before matching (see the
  `wes-reload` skill).
- **`LastTaskResult` is not trustworthy** (#032): the launcher's exit code is
  PowerShell's, not the child python's, so a process that never started still
  records `0`. Read the log, not the result code.
- The specific `Z:\` boot race that #032 opened on is gone with the share, but
  a broken venv or a bad launcher edit fails exactly as silently.

## 4. Is the model loaded and pinned?

```powershell
& C:\Users\awarm\wes-pc\wes-dev.ps1 models status
& C:\Users\awarm\wes-pc\wes-dev.ps1 models check
```
- Expect `gemma4:12b` **resident** with a far-future `until:` — that's
  `keep_alive=-1`, i.e. pinned. The server's `warmup()` pins it on start, so
  `RESIDENT (none)` is usually a symptom that **the server is down**, not a
  pinning problem.
- `models check` must report `all configured models present` — it's the gate on
  the config-vs-reality drift that once fell back to Claude for a week.

## 5. Are the metrics fresh?

```powershell
Get-Content C:\Users\awarm\wes\tests\perf_history_text.csv -Tail 3
Get-Content C:\Users\awarm\wes\tests\eval_history.csv -Tail 1
```
- Both should have a row from **last night** (nightly runs 03:30). A gap means
  the nightly failed — check `logs/eval_last.log`. A gap is the cheapest signal
  that the server was down, since the eval needs a live server.
- Dashboard: <http://localhost:3001>; Prometheus targets:
  <http://127.0.0.1:9090/targets> (all `up`).
- If every PC panel is blank at once but Grafana itself is fine, the WSL NAT
  gateway moved — see the `windows-host` note in
  `observability/docker-compose.yml`.
- Note: the Discord alert path **cannot** report a whole-PC outage — the bot
  dies with everything else, and Prometheus firing `TargetDown` at a bot that
  isn't running changes nothing. Absent alerts are not evidence of health.

## 6. Recover, then verify

Only step that writes. If a service is down:

```powershell
& C:\Users\awarm\wes-pc\wes-dev.ps1 reload           # WES Server
& C:\Users\awarm\wes-pc\wes-dev.ps1 reload discord
```
Startup is a few seconds now that there are no STT/TTS models to load. Then
confirm the baseline still holds:

```powershell
& C:\Users\awarm\wes-pc\.venv\Scripts\python.exe -m pytest C:\Users\awarm\wes\tests\ -q `
    --ignore=C:\Users\awarm\wes\tests\test_e2e.py
& C:\Users\awarm\wes-pc\.venv\Scripts\python.exe C:\Users\awarm\wes\tests\perf_check.py
```
- A cold-loaded model inflates the first run; a single measurement near the
  threshold right after a restart is expected, not a regression.

## 7. Read the queue

`docs/tickets/INDEX.md` for open work; `docs/keyresults.md` for the current
cycle's targets. If either reads as stale relative to today's date, fixing it is
part of the session, not a distraction.
