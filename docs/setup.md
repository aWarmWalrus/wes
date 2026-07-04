# Setup & operations

## Tier 2 (PC) — local to `C:`, NOT on `Z:\`

- venv: `C:\Users\awarm\wes-pc\.venv` (Windows; `Z:\` is the Pi's Linux share).
- voice model: `C:\Users\awarm\wes-pc\voices\en_GB-alan-medium.onnx` (British male,
  22050 Hz — matches the Pi player). `en_US-amy-medium` also present but unused.
- deps: `flask anthropic faster-whisper piper-tts pychromecast pytest` **plus two pins**:
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

## Tier 1 (Pi) client

Deps in `~/wes/.venv` (openwakeword, pyaudio, requests, numpy); the "hey_jarvis" model
is downloaded. Run:
```bash
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
