---
name: wes-reload
description: Reload, restart, or debug the live WES services (server, Discord bot) and read their logs. Use after editing pc/wes_server.py, pc/wes_discord.py, or the PC-local launchers.
---

# Reloading and debugging the live WES services

Both services run as user scheduled tasks on the PC (hidden at logon,
auto-restart 3x on failure). Launchers are PC-local, NOT in the repo:
`C:\Users\awarm\wes-pc\run_server.ps1` / `run_discord.ps1` (they set env and
pull secrets from the user environment).

## Reload after a code change

**Use the allowlisted helper (no permission prompt):**

```powershell
& C:\Users\awarm\wes-pc\wes-dev.ps1 reload            # WES Server (default) + waits for /health
& C:\Users\awarm\wes-pc\wes-dev.ps1 reload discord    # WES Discord
& C:\Users\awarm\wes-pc\wes-dev.ps1 reload exporters  # WES Exporters
```

The raw equivalent (only if you need it):

```powershell
Stop-ScheduledTask -TaskName "WES Server"; Start-Sleep 2
Start-ScheduledTask -TaskName "WES Server"
```

(Same pattern with `"WES Discord"` for the bot.)

- The server warms up Whisper + piper + Gemma BEFORE serving — allow **60-120s**
  before `/health` answers. Poll with a generous deadline; do not conclude
  failure at 45s.
- Verify: `Invoke-RestMethod http://127.0.0.1:8080/health` — the `llm` field
  shows the router + escalation target (single-model topology since 2026-07-16,
  e.g. `local (gemma4:12b) + gemma4:12b escalation (thinking)`).

## Reading the logs

`C:\Users\awarm\wes-pc\logs\server.log`, `discord.log`, `eval.log`. Task
redirection writes them UTF-16-ish; `Select-String` and `-match` miss
content unless nulls are stripped first:

```powershell
$log = Get-Content C:\Users\awarm\wes-pc\logs\server.log -Raw
($log -replace "`0","") -split "`n" | Where-Object { $_ -match 'escalat' }
```

Useful log markers: `[route] escalating to ...`, `[tool] name(args)`,
`[respond_text] (channel)`, `[timing] stt=..`, `[discord] logged in as`.

## Testing server changes without touching the live service

Run a second instance on another port (models load twice — fine briefly):

```powershell
$env:WES_PORT = "8081"
C:\Users\awarm\wes-pc\.venv\Scripts\python.exe C:\Users\awarm\wes\pc\wes_server.py
# ...exercise http://127.0.0.1:8081 ... then stop it:
$c = Get-NetTCPConnection -LocalPort 8081 -State Listen
Stop-Process -Id $c[0].OwningProcess -Force -Confirm:$false
```

## Pi client

Runs as a systemd **user** unit (`wes-client`, from `pi/wes-client.service`;
linger enabled so it survives reboots). After editing `pi/wes_client.py`,
`pi/pi_state.py`, or `pi/hailo_*.py`:

```bash
ssh walrus-pi "systemctl --user restart wes-client"
ssh walrus-pi "journalctl --user -u wes-client -n 30 --no-pager -o cat"
```

- Startup takes ~10s (wake-word model load); verify with the `[state] endpoint
  on :8090` log line or `curl http://10.0.0.79:8090/state`.
- A restart drops the JBL's A2DP connection; `bt_monitor` reclaims it within
  ~5-10s automatically. Repeated `[bt] connect rc=1` /
  `le-connection-abort-by-local` means the speaker is off or out of battery —
  not a software problem.

## Observability stack (docs/observability.md)

- PC metrics exporters run under the `"WES Exporters"` scheduled task
  (launcher `run_exporters.ps1`, binaries in `wes-pc\bin\`); same
  Stop/Start-ScheduledTask reload pattern. Logs: `logs\exporters.log`,
  `logs\windows_exporter.err.log`.
- Pi side: `ssh walrus-pi "sudo systemctl restart prometheus"` (or
  `grafana-server`, `prometheus-node-exporter`). Target health:
  <http://10.0.0.79:9090/targets>; dashboard <http://10.0.0.79:3000>.
- The dashboard JSON is provisioned from the repo
  (`observability/dashboards/wes-overview.json`) — edit there, copy to
  `/var/lib/grafana/dashboards/`, restart grafana-server.

## Smoke-test via the scheduled task, not just the script

**The scheduled-task environment differs from an interactive shell — verify
service changes on the task, never only by running the .py directly.** Two
production bugs came from exactly this gap (both 2026-07-05): the "WES
Exporters" task launched without `-WindowStyle Hidden` (a visible console that,
when closed, killed the service with no auto-restart), and the Discord bot's
alert watcher died on its first real alert because the task's stdout is
**cp1252** — `print()`-ing an emoji raised `UnicodeEncodeError`. Neither
reproduces when you run the script in your own UTF-8 terminal.

So after editing a service:
1. Reload the **task** (not `python wes_*.py` in your shell), then
2. exercise the real path and read the **task's** log
   (`logs\server.log` / `discord.log`, UTF-16-ish — strip nulls as above), and
3. for anything that prints user/dynamic content or runs in the background,
   confirm it survived (look for the expected log line, not just absence of a
   crash — the cp1252 death was silent).

Rules for new service code: keep console `print()`s ASCII-safe (use `!a` or
reconfigure the stream `errors="replace"` in `main`, as both services now do);
give every new scheduled task `-WindowStyle Hidden`.

## Cautions

- Restarting "WES Server" briefly takes the house assistant down — routine
  after edits, but don't restart on a hunch mid-conversation with the user.
- The audio house rule still applies: never trigger speaker playback (cast or
  JBL) as part of debugging without explicit confirmation. `/respond_text` is
  the safe silent probe.
- The Pi client is separate (`ssh walrus-pi`, runs `pi/wes_client.py` from
  `~/wes/.venv`); server reloads don't require touching it.
