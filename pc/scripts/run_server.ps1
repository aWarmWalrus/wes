# WES Tier 2 launcher — started at logon by the "WES Server" scheduled task.
# Sets the piper/voice env and runs the Flask service, logging to logs\server.log.
# ANTHROPIC_API_KEY is read from the user environment (set via setx).

$base = "C:\Users\awarm\wes-pc"
$py = "$base\.venv\Scripts\python.exe"
$server = "Z:\wes\pc\wes_server.py"
$logdir = "$base\logs"

New-Item -ItemType Directory -Force -Path $logdir | Out-Null

$env:WES_PIPER_BIN = "$base\.venv\Scripts\piper.exe"
$env:WES_VOICE_MODEL = "$base\voices\en_GB-cori-medium.onnx"  # British female (Cori)
$env:WES_WHISPER_MODEL = "tiny.en"   # faster than base.en on CPU
$env:WES_LLM_RPM = "50"              # Tier 1 Haiku ~50 rpm (was assuming 5)
# SINGLE-MODEL topology (2026-07-16). Was a split: e4b as the fast router +
# 12b for vision/escalation. gemma4:e4b was removed from Ollama at some point
# before 2026-07-09, so every router call 404'd and silently fell back to
# Claude Haiku for a week (/health only echoes config, so it still said "ok").
# Caught by: wes-dev.ps1 models check -- run it after ANY model change.
#
# Collapsed to 12b alone rather than restoring e4b: the split only existed to
# protect voice time-to-first-audio and to serve vision, and both premises are
# being retired (moving toward batch/analysis; camera work paused). gemma4:12b
# is the only installed model with 'tools' besides gemma4:latest -- gemma3:4b
# has vision but NO tools, so it can never be the router.
#
# Known cost, measured A/B 2026-07-04 (docs/pipeline.md): 12b-as-router adds
# +44% chat ttfa / +63% tool-turn latency vs e4b, for zero eval-quality gain.
# Accepted deliberately now that voice latency is not the priority.
# Buys back ~3.3GB VRAM (one model ~7GB, not ~11.4GB) -> headroom for analysis.
# To restore a fast router: pull a tool-capable small model, point
# WES_LLM_LOCAL_MODEL at it, and re-run `wes-dev.ps1 models fit`.
# Claude remains the error fallback only; set WES_LLM = "claude" to switch back.
$env:WES_LLM = "local"
$env:WES_LLM_LOCAL_MODEL = "gemma4:12b"
$env:WES_VLM_MODEL = "gemma4:12b"
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
