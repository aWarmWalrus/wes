# Setup & operations

Everything runs on the PC. There was a tier-1 Raspberry Pi section here until
2026-09-02, covering the client venv, the mic, Bluetooth and the Hailo/system-
python split; that hardware was repurposed and the section went with it. See
`archive/pi/README.md`, and `git log -- docs/setup.md` if you need the original
provisioning steps back.

## The PC environment

- venv: `C:\Users\awarm\wes-pc\.venv`, built from the `E:\miniconda3` base.
- deps: **`pc/requirements.txt`** is the pinned list (direct deps only;
  transitive float). Rebuild with
  `...\.venv\Scripts\python.exe -m pip install -r C:\Users\awarm\wes\pc\requirements.txt`,
  then `playwright install chromium`.
  `requirements-dev.txt` (repo root) is the lighter set CI installs.
  - **The speech stack is gone**, and with it the two pins that used to be the
    most load-bearing lines in this file: `ctranslate2==4.4.0` (4.8.0 segfaulted
    with `0xC0000005` on Whisper model load on this machine) and
    `onnxruntime==1.20.1` (1.27.0's DLL init failed on Windows; piper needed
    onnxruntime). `faster-whisper`, `piper-tts`, `PyChromecast` and `numpy` went
    at the same time. If speech ever returns, `git log -- pc/requirements.txt`
    has the detail — those two versions were found the hard way.
- `ANTHROPIC_API_KEY` lives only in the PC **user environment** (`setx`), never
  in the repo. With no key, escalation and web search degrade rather than fail.
  Rate limit knob `WES_LLM_RPM` (launcher sets 50).
- Ollama models live in `E:\.ollama\models`. That path is set in the Ollama
  desktop app's own `db.sqlite`, which **overrides** the `OLLAMA_MODELS`
  environment variable — changing the env var alone does nothing.

Manual run (the scheduled task normally does this):
```powershell
C:\Users\awarm\wes-pc\.venv\Scripts\python.exe C:\Users\awarm\wes\pc\wes_server.py
```

### Auto-start service (Task Scheduler)

Scheduled task **"WES Server"** starts hidden at logon and auto-restarts on
failure — `RestartCount` is **999**, and that is not a tuning knob: at 3 it
burned through its retries and stayed dead for a week (`docs/observability.md`).
Launcher `C:\Users\awarm\wes-pc\run_server.ps1` reads `ANTHROPIC_API_KEY` from
the user env and logs to `C:\Users\awarm\wes-pc\logs\server.log` (reset each
start). On startup the server `warmup()`s Gemma into VRAM (~2.5s) **before**
serving, so the first request is warm. It used to warm Whisper and the piper
voice too, which was most of that time.

```powershell
Start-ScheduledTask   -TaskName "WES Server"   # start now
Stop-ScheduledTask    -TaskName "WES Server"   # stop
Get-ScheduledTaskInfo -TaskName "WES Server"   # status
# after editing wes_server.py: Stop then Start to reload
```

Still Flask's dev server — fine for home use; swap for waitress/NSSM to harden later.

### Discord bot (`pc/wes_discord.py`)

Remote text access to Jarvis (roadmap "Remote access via Discord"). One-time
setup:

1. [discord.com/developers/applications](https://discord.com/developers/applications)
   → New Application → **Bot** tab: copy the token. No privileged intents
   needed — Discord always delivers message content for DMs and @mentions,
   the bot's only two paths.
2. Invite it: OAuth2 → URL Generator → scope `bot`, permissions Send Messages →
   open the URL (or just DM the bot — DMs need no server).
3. Your user ID: Discord Settings → Advanced → Developer Mode, then
   right-click your name → Copy User ID.

```powershell
setx WES_DISCORD_TOKEN "<bot token>"        # user env, like ANTHROPIC_API_KEY
setx WES_DISCORD_OWNER_ID "<your user ID>"  # the ONLY user the bot answers
# run (new shell so setx is visible); same venv as the server, discord.py installed
C:\Users\awarm\wes-pc\.venv\Scripts\python.exe C:\Users\awarm\wes\pc\wes_discord.py
```

DM the bot (or @mention it in a server you share). `!reset` starts a new
conversation. Replies come from `POST /respond_text` on the local server. This
is the only interactive way into WES now that the voice tier is retired, so the
"WES Discord" scheduled task mirroring "WES Server" is not optional — note its
`RestartCount` is still **3**, and raising it needs an elevated prompt
(`docs/observability.md`).

### Nightly eval task

Scheduled task **"WES Nightly Eval"** (daily 3:30 AM) runs
`C:\Users\awarm\wes-pc\run_nightly_eval.ps1`: `perf_check.py` +
`eval_turns.py --judge local` against the live server; one verdict line per
night in `C:\Users\awarm\wes-pc\logs\eval.log`, full output in
`logs\eval_last.log`. Details: `docs/eval-design.md` §7. To re-register:

```powershell
$action = New-ScheduledTaskAction -Execute "powershell.exe" `
  -Argument '-NoProfile -ExecutionPolicy Bypass -File C:\Users\awarm\wes-pc\run_nightly_eval.ps1'
Register-ScheduledTask -TaskName "WES Nightly Eval" -Action $action `
  -Trigger (New-ScheduledTaskTrigger -Daily -At 3:30AM)
```

### Fantasy GM task (#029 P3)

Scheduled task **"WES Fantasy GM"** — four triggers, all Pacific:
- **daily 6:00 AM** — covers the Tuesday weekly-waiver clear + general injury news
- **Sunday 9:15 AM** — the main slate locks 10:00 AM PT (~45min pre-lock)
- **Thursday + Monday 4:30 PM** — TNF/MNF kick 8:15 PM ET = 5:15 PM PT (same
  ~45min lead). Added 2026-07-31; without these, a Thursday-night starter ruled
  out during the day was only caught by the following 6 AM run, hours too late.

Runs
`C:\Users\awarm\wes-pc\run_fantasy_gm.ps1`: `pc\fantasy_gm_run.py` calls
`wes_execute.propose_lineup_change` for every configured team whose autonomy is
`propose` or `auto` (never `advise` — that mode can't act, so running it would
just be a wasted scrape). **Log-only by design**, same house rule as Nightly
Eval — WES never speaks on its own initiative for a routine run. One line per
run in `logs\fantasy_gm.log`; full output in `logs\fantasy_gm_last.log`. A REAL
Yahoo write is marked `[EXECUTED]` in the detail log so it's grep-able, but
nothing DMs the owner about it yet — a deliberately deferred next step (ticket
#029), not an oversight. To re-register:

```powershell
$action = New-ScheduledTaskAction -Execute "powershell.exe" `
  -Argument '-NoProfile -ExecutionPolicy Bypass -File C:\Users\awarm\wes-pc\run_fantasy_gm.ps1'
$principal = New-ScheduledTaskPrincipal -UserId "awarm" -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName "WES Fantasy GM" -Action $action -Principal $principal `
  -Trigger @((New-ScheduledTaskTrigger -Daily -At "06:00"),
             (New-ScheduledTaskTrigger -Weekly -DaysOfWeek Sunday -At "09:15"),
             (New-ScheduledTaskTrigger -Weekly -DaysOfWeek Thursday -At "16:30"),
             (New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday -At "16:30"))
```

To add triggers to the EXISTING task without re-registering it (how the
Thu/Mon ones were added — `Set-ScheduledTask` replaces the whole trigger set,
so pass the existing ones back too):

```powershell
$t = Get-ScheduledTask -TaskName "WES Fantasy GM"
Set-ScheduledTask -TaskName "WES Fantasy GM" -Trigger ($t.Triggers +
  (New-ScheduledTaskTrigger -Weekly -DaysOfWeek Thursday -At "16:30"))
```

## Monitoring stack (Docker)

Prometheus + Grafana, defined by `observability/docker-compose.yml`. Docker on
this machine runs **inside WSL** and needs `sudo`, so:

```powershell
& C:\Users\awarm\wes-pc\wes-dev.ps1 obs "up -d"     # start
& C:\Users\awarm\wes-pc\wes-dev.ps1 obs ps
```

First-time setup is two steps:

1. Copy `observability/.env.example` to `observability/.env` and set
   `WES_WINDOWS_HOST` to the WSL default gateway
   (`wsl -e bash -lc 'ip route show default'`). That address is how the
   containers reach the Windows-side exporters, and it changes when WSL
   restarts.
2. Run `observability/allow-wsl-scrape.ps1` **elevated**, once. Without it the
   Windows firewall silently drops every scrape and three targets go down at
   once while Grafana looks fine.

Both failure modes, and how to tell them apart, are in `docs/observability.md`.
