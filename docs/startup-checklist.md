# Session startup checklist

Run this at the top of a working session (and after any reboot or power event)
**before** trusting anything about the system's state. It exists because WES is
three hosts with no supervisor above them: a service can be dead for a day
while `/health` is never asked, the nightly eval quietly no-ops, and nothing
tells you. That happened on 2026-07-28 (see `docs/tickets/` #032).

Everything here is read-only except step 6.

## 1. Repo state

```bash
cd /z/wes && git status --short && git log --oneline -5
```
- Uncommitted work is normal mid-feature, but know what it is before you build
  on it. `tests/eval_history.csv` + `tests/perf_history_stream.csv` churn on
  their own — the nightly appends to them.

Then check the launchers the services actually run match the repo:

```powershell
& C:\Users\awarm\wes-pc\wes-dev.ps1 deploy check
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
- Pi side:
  ```bash
  ssh walrus-pi "systemctl --user is-active wes-client; uptime"
  curl -s http://10.0.0.79:8090/state
  ```

## 3. Did anything crash at boot?

If the PC rebooted recently, the launchers may have lost the race with the
`Z:\` drive mapping (that's #032 — `can't open file 'Z:\\wes\\pc\\...'`).

```powershell
(Get-CimInstance Win32_OperatingSystem).LastBootUpTime
Get-ScheduledTaskInfo -TaskName 'WES Server' | Select-Object LastRunTime,LastTaskResult
Get-Content C:\Users\awarm\wes-pc\logs\server.log  -Tail 15
Get-Content C:\Users\awarm\wes-pc\logs\discord.log -Tail 15
```
- Task-redirected logs are UTF-16-ish; strip nulls before matching (see the
  `wes-reload` skill).
- `net use Z:` should say `Status OK` — if it doesn't, nothing on the PC can
  reach the repo.

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

```bash
tail -3 /z/wes/tests/perf_history_stream.csv
tail -1 /z/wes/tests/eval_history.csv
```
- Both should have a row from **last night** (nightly runs 03:30). A gap means
  the nightly failed — check `logs/eval_last.log`. A gap is the cheapest signal
  that the server was down, since the eval needs a live server.
- Dashboard: <http://10.0.0.79:3000>; Prometheus targets:
  <http://10.0.0.79:9090/targets> (all `up`).
- Note: the Discord alert path **cannot** report a whole-PC outage — the bot
  dies with everything else. Absent alerts are not evidence of health.

## 6. Recover, then verify

Only step that writes. If a service is down:

```powershell
& C:\Users\awarm\wes-pc\wes-dev.ps1 reload           # WES Server
& C:\Users\awarm\wes-pc\wes-dev.ps1 reload discord
```
Warmup is 60-120s — poll `/health` with a generous deadline, don't call it dead
at 45s. Then confirm the baseline still holds:

```powershell
& C:\Users\awarm\wes-pc\.venv\Scripts\python.exe -m pytest Z:\wes\tests\ -q `
    --ignore=Z:\wes\tests\test_e2e.py --ignore=Z:\wes\tests\test_faces.py
& C:\Users\awarm\wes-pc\.venv\Scripts\python.exe Z:\wes\tests\perf_check.py
```
- A cold-loaded model inflates the first `ttfa_ms`; a single run near the
  threshold right after a restart is expected, not a regression.
- House rule: never trigger speaker playback while checking. `/respond_text`
  is the silent probe.

## 7. Read the queue

`docs/tickets/INDEX.md` for open work; `docs/keyresults.md` for the current
cycle's targets. If either reads as stale relative to today's date, fixing it is
part of the session, not a distraction.
