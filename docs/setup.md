# Setup & operations

## Tier 2 (PC) — local to `C:`, NOT on `Z:\`

- venv: `C:\Users\awarm\wes-pc\.venv` (Windows; `Z:\` is the Pi's Linux share).
- voice model: `C:\Users\awarm\wes-pc\voices\en_GB-alan-medium.onnx` (British male,
  22050 Hz — matches the Pi player). `en_US-amy-medium` also present but unused.
- deps: **`pc/requirements.txt`** is the pinned list (direct deps only;
  transitive float). Rebuild with
  `...\.venv\Scripts\python.exe -m pip install -r Z:\wes\pc\requirements.txt`,
  then `playwright install chromium`.
  `requirements-dev.txt` (repo root) is the lighter set CI installs — it
  deliberately leaves `ctranslate2` unpinned, since 4.4.0 has no Linux wheel.
  Two pins are **load-bearing** — don't bump without re-testing on this PC:
  - **`ctranslate2==4.4.0`** — 4.8.0 segfaults (`0xC0000005`) on model load on this PC.
  - **`onnxruntime==1.20.1`** — 1.27.0's DLL init fails (piper needs onnxruntime).
- STT runs on **CPU int8** by default (`WES_WHISPER_MODEL=tiny.en` in the launcher).
  `WES_WHISPER_DEVICE=cuda` opts into the 1660 once cuBLAS/cuDNN are installed
  (unsupported CUDA hard-aborts).
- `ANTHROPIC_API_KEY` lives only in the PC **user environment** (`setx`), never in the
  repo (`Z:\` is the Pi share). With no key, the PC echoes the transcript (STT+TTS still
  testable). Rate limit knob `WES_LLM_RPM` (launcher sets 50).

Manual run (the scheduled task normally does this):
```powershell
$env:WES_PIPER_BIN="C:\Users\awarm\wes-pc\.venv\Scripts\piper.exe"
$env:WES_VOICE_MODEL="C:\Users\awarm\wes-pc\voices\en_GB-alan-medium.onnx"
C:\Users\awarm\wes-pc\.venv\Scripts\python.exe Z:\wes\pc\wes_server.py
```

### Auto-start service (Task Scheduler)

Scheduled task **"WES Server"** starts hidden at logon, auto-restarts 3× on failure.
Launcher `C:\Users\awarm\wes-pc\run_server.ps1` sets the piper/voice env, reads
`ANTHROPIC_API_KEY` from the user env, and logs to
`C:\Users\awarm\wes-pc\logs\server.log` (reset each start). On startup the server
`warmup()`s Whisper + the piper voice + Gemma (~4.5s) **before** serving, so the first
request is warm.

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
C:\Users\awarm\wes-pc\.venv\Scripts\python.exe Z:\wes\pc\wes_discord.py
```

DM the bot (or @mention it in a server you share). `!reset` starts a new
conversation. Replies come from `POST /respond_text` on the local server —
text only, no audio path. Register a "WES Discord" scheduled task mirroring
"WES Server" once it proves itself.

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

Scheduled task **"WES Fantasy GM"** (daily 6:00 AM PT + Sunday 9:15 AM PT — the
main NFL slate locks 10:00 AM PT, so this is a ~45min pre-lock check; 6 AM
covers the Tuesday weekly-waiver clear + general injury news) runs
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
             (New-ScheduledTaskTrigger -Weekly -DaysOfWeek Sunday -At "09:15"))
```

## Tier 1 (Pi) client

Deps in `~/wes/.venv` — pinned list in **`pi/requirements.txt`**
(`~/wes/.venv/bin/python -m pip install -r ~/claude/wes/pi/requirements.txt`).
Includes **pyyaml** for the `hosts.yaml` registry; the client falls back to the
hard-coded PC address if it's missing. The "hey_jarvis" model is downloaded.
That file covers **only this venv** — the Hailo/vision scripts run under the
Pi's system `python3` (hailo_platform + cv2, provisioned via apt/the Hailo
installer), and casting uses a separate `~/cast-venv`.

Runs as a **systemd user unit** since 2026-07-05 (`pi/wes-client.service`,
installed to `~/.config/systemd/user/`; linger is enabled so it starts at boot
— a user unit, not system, because audio needs the session's pipewire):
```bash
systemctl --user restart wes-client     # reload after editing pi/wes_client.py
systemctl --user status wes-client
journalctl --user -u wes-client -n 50   # logs
# manual run (service stopped) for debugging:
~/wes/.venv/bin/python ~/claude/wes/pi/wes_client.py
```
- Mic: `MIC_DEVICE_INDEX = 0` → PulseAudio default source (the Pi exposes `pulse`/
  `default`, not the C920 directly). Adjust if the wrong input is picked up.
- The client also starts `pi_state.py`'s read-only endpoint on `:8090`.

## Pi environment notes

- OS: Debian Bookworm, kernel 6.12 (aarch64). Onboard BT 5.0, adapter `hci0`, MAC
  `88:A2:9E:A6:BA:F4`. `rfkill` is not installed — use `hciconfig`/`bluetoothctl`.
- Killing client processes: use a bracketed pattern (`pkill -f '[w]es_client.py'`) so
  the `pkill`/`ssh` shell doesn't match and kill itself.
