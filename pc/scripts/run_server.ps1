# WES server launcher — started at logon by the "WES Server" scheduled task.
# Runs the Flask service, logging to logs\server.log.
# ANTHROPIC_API_KEY is read from the user environment (set via setx).
#
# WES_PIPER_BIN / WES_VOICE_MODEL / WES_WHISPER_MODEL were set here until
# 2026-09-02. The Pi that fed the microphone and played the speaker was
# repurposed, so STT and TTS came out of the server entirely and the server no
# longer reads any of the three (archive/pi/README.md).

$base = "C:\Users\awarm\wes-pc"
# Code runs from a LOCAL clone. It briefly ran from the Z: share (the Pi's
# clone over Samba), which made the PC unable to boot its services without the
# Pi (#032) and which execution policy refused to run unsigned scripts from at
# all. WES_REPO overrides for a different checkout.
$repo = if ($env:WES_REPO) { $env:WES_REPO } else { "C:\Users\awarm\wes" }
$py = "$base\.venv\Scripts\python.exe"
$server = "$repo\pc\wes_server.py"
$logdir = "$base\logs"

New-Item -ItemType Directory -Force -Path $logdir | Out-Null

$env:WES_LLM_RPM = "50"              # Tier 1 Haiku ~50 rpm (was assuming 5)
# SINGLE-MODEL topology (2026-07-16). Was a split: e4b as the fast router +
# 12b for vision/escalation. gemma4:e4b was removed from Ollama at some point
# before 2026-07-09, so every router call 404'd and silently fell back to
# Claude Haiku for a week (/health only echoes config, so it still said "ok").
# Caught by: wes-dev.ps1 models check -- run it after ANY model change.
#
# Collapsed to 12b alone rather than restoring e4b: the split only existed to
# protect voice time-to-first-audio and to serve vision, and BOTH premises are
# now gone outright -- the voice tier and the camera retired with the Pi on
# 2026-09-02. gemma4:12b is the only installed model with 'tools' besides
# gemma4:latest.
#
# Known cost, measured A/B 2026-07-04: 12b-as-router added +44% chat ttfa /
# +63% tool-turn latency vs e4b, for zero eval-quality gain. That was a real
# tradeoff while a person was waiting for speech; with only Discord and the
# scheduled fantasy runs left, nothing is waiting on first-token latency.
# Buys back ~3.3GB VRAM (one model ~7GB, not ~11.4GB) -> headroom for analysis.
# To restore a fast router: pull a tool-capable small model, point
# WES_LLM_LOCAL_MODEL at it, and re-run `wes-dev.ps1 models fit`.
# Claude remains the error fallback only; set WES_LLM = "claude" to switch back.
#
# WES_VLM_MODEL is no longer read -- the only caller was describe_scene, which
# needed a camera.
$env:WES_LLM = "local"
$env:WES_LLM_LOCAL_MODEL = "gemma4:12b"
$env:WES_ESCALATE_MODEL = "gemma4:12b"
# Ensure the key is present even if the task's env snapshot is stale.
if (-not $env:ANTHROPIC_API_KEY) {
    $env:ANTHROPIC_API_KEY = [System.Environment]::GetEnvironmentVariable("ANTHROPIC_API_KEY", "User")
}

# LIVE WRITES to Yahoo fantasy rosters (#029 P3, wes_execute.py). ON since
# 2026-07-30 (owner: "turn it on! live writes for real is what I want") --
# also persisted via `setx WES_YAHOO_LIVE_WRITES 1`, but hardcoded here too so
# the live service never depends on a scheduled task's env-snapshot timing.
# This is a REAL kill switch for a REAL write path: with it on, any team whose
# teams.yaml autonomy is `auto` (today: only nfl.l.957011.t.4, "Charles's Pop",
# deliberately chosen because the owner does not care about that league's
# outcome) will have its roster ACTUALLY CHANGED on Yahoo when
# fantasy_propose_lineup_change runs and the guardrails allow it -- reachable
# today only via that tool being called in conversation; #005 (scheduling)
# doesn't exist yet, so nothing runs this unattended. To disable: set to "0"
# here (or delete this line) and `setx WES_YAHOO_LIVE_WRITES 0`, then reload.
$env:WES_YAHOO_LIVE_WRITES = "1"

$stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
# Reset the log on each start so it doesn't grow unbounded across restarts.
Set-Content "$logdir\server.log" "==== WES server starting $stamp ===="
& $py $server *>> "$logdir\server.log"
