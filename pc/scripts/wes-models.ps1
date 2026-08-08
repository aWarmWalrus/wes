# WES model/VRAM manager — inspect and control what Ollama holds in VRAM, and
# catch config/reality DRIFT.
#
# Drift is why this exists: WES_LLM_LOCAL_MODEL pointed at "gemma4:e4b" after
# that model was gone, Ollama 404'd every router call, and the server silently
# fell back to Claude for a week. /health reported "ok" the whole time because it
# echoes CONFIG, not reality. `check` is the guard that would have caught it.
#
# Driven via the dev helper so it inherits the single allowlist rule:
#   & C:\Users\awarm\wes-pc\wes-dev.ps1 models [status|check|list|load|unload|fit]

param(
    [string]$cmd = "status",
    [Parameter(ValueFromRemainingArguments = $true)]$rest
)
$ErrorActionPreference = "Stop"
if ($null -eq $rest) { $rest = @() }

$OLLAMA = "http://127.0.0.1:11434"
$RUNNER = "C:\Users\awarm\wes-pc\run_server.ps1"
# Only these env vars name an Ollama model. WES_WHISPER_MODEL / WES_VOICE_MODEL
# are STT/TTS assets and WES_LLM_MODEL is the Claude id — none are Ollama tags.
$LLM_VARS = @("WES_LLM_LOCAL_MODEL", "WES_ESCALATE_MODEL", "WES_VLM_MODEL")

function Get-Installed {
    try { return (Invoke-RestMethod "$OLLAMA/api/tags" -TimeoutSec 5).models }
    catch { throw "ollama unreachable at $OLLAMA -- $($_.Exception.Message)" }
}
function Get-Loaded {
    try { return (Invoke-RestMethod "$OLLAMA/api/ps" -TimeoutSec 5).models }
    catch { return @() }
}
function Get-Caps($name) {
    try {
        $b = @{ model = $name } | ConvertTo-Json
        $r = Invoke-RestMethod "$OLLAMA/api/show" -Method Post -Body $b -ContentType "application/json" -TimeoutSec 15
        if ($null -eq $r.capabilities) { return @() }
        return $r.capabilities
    } catch { return @() }
}
function Get-Vram {
    $o = nvidia-smi --query-gpu=memory.used,memory.total --format=csv,noheader,nounits
    $p = ($o -split ",") | ForEach-Object { [int]$_.Trim() }
    return [pscustomobject]@{ UsedMB = $p[0]; TotalMB = $p[1]; FreeMB = ($p[1] - $p[0]) }
}
function Get-WesConfig {
    # run_server.ps1 defines the scheduled task's env, so it is the source of
    # truth for what the server will actually ask Ollama for.
    $cfg = [ordered]@{}
    if (-not (Test-Path $RUNNER)) { return $cfg }
    foreach ($line in (Get-Content $RUNNER)) {
        $m = [regex]::Match($line, '^\s*\$env:(WES_[A-Z0-9_]+)\s*=\s*"([^"]+)"')
        if ($m.Success -and ($LLM_VARS -contains $m.Groups[1].Value)) {
            $cfg[$m.Groups[1].Value] = $m.Groups[2].Value
        }
    }
    return $cfg
}
function ToGB($bytes) { return [math]::Round($bytes / 1GB, 1) }
function Resolve-Model($n) {
    # accept "gemma4:12b" or bare "gemma4" (ollama implies :latest)
    if ($n -match ":") { return $n }
    return "$n`:latest"
}

switch ($cmd) {

    "status" {
        $v = Get-Vram
        $pct = [math]::Round(100 * $v.UsedMB / $v.TotalMB)
        "VRAM   $($v.UsedMB) / $($v.TotalMB) MiB used ($pct%), $($v.FreeMB) MiB free"
        ""
        $loaded = Get-Loaded
        if ($loaded.Count -eq 0) {
            "RESIDENT  (none -- next request will cold-load)"
        } else {
            "RESIDENT"
            foreach ($m in $loaded) {
                $until = if ($m.expires_at -match "^0001") { "forever" } else { $m.expires_at }
                "  {0,-18} {1,6} GB vram   until: {2}" -f $m.name, (ToGB $m.size_vram), $until
            }
        }
        ""
        "WES CONFIG vs REALITY"
        $names = (Get-Installed | ForEach-Object { $_.name })
        $cfg = Get-WesConfig
        if ($cfg.Count -eq 0) { "  (could not read $RUNNER)"; break }
        foreach ($k in $cfg.Keys) {
            $want = Resolve-Model $cfg[$k]
            $isLoaded = ($loaded | Where-Object { $_.name -eq $want })
            if ($names -contains $want) {
                $state = if ($isLoaded) { "installed, resident" } else { "installed, not resident" }
                "  OK    {0,-22} -> {1,-16} ({2})" -f $k, $cfg[$k], $state
            } else {
                "  DRIFT {0,-22} -> {1,-16} (NOT INSTALLED -- this silently falls back to Claude)" -f $k, $cfg[$k]
            }
        }
    }

    "check" {
        # exit 1 on drift so this can gate a warmup / feed an alert rule
        $names = (Get-Installed | ForEach-Object { $_.name })
        $cfg = Get-WesConfig
        $bad = 0
        foreach ($k in $cfg.Keys) {
            $want = Resolve-Model $cfg[$k]
            if ($names -contains $want) {
                "OK    $k -> $($cfg[$k])"
            } else {
                "DRIFT $k -> $($cfg[$k]) NOT INSTALLED"
                $bad++
            }
        }
        if ($bad -gt 0) { "`n$bad configured model(s) missing."; exit 1 }
        "`nall configured models present."
    }

    "list" {
        "{0,-18} {1,7}  {2}" -f "MODEL", "SIZE", "CAPABILITIES"
        foreach ($m in (Get-Installed | Sort-Object name)) {
            $caps = Get-Caps $m.name
            $c = if ($caps.Count -gt 0) { $caps -join "," } else { "-" }
            # tools is the one that matters for the router -- gemma3:4b lacks it
            "{0,-18} {1,6} GB  {2}" -f $m.name, (ToGB $m.size), $c
        }
        ""
        "note: a router model MUST have 'tools'."
    }

    "load" {
        if ($rest.Count -lt 1) { return "usage: models load <model> [<model> ...]" }
        foreach ($n in $rest) {
            $want = Resolve-Model $n
            $b = @{ model = $want; prompt = ""; keep_alive = -1 } | ConvertTo-Json
            try {
                Invoke-RestMethod "$OLLAMA/api/generate" -Method Post -Body $b -ContentType "application/json" -TimeoutSec 300 | Out-Null
                $v = Get-Vram
                "loaded $want (keep_alive=forever) -- VRAM now $($v.UsedMB)/$($v.TotalMB) MiB"
            } catch { "FAILED to load $want -- $($_.Exception.Message)" }
        }
    }

    "unload" {
        $targets = @()
        if ($rest.Count -lt 1 -or $rest[0] -eq "all") {
            $targets = (Get-Loaded | ForEach-Object { $_.name })
            if ($targets.Count -eq 0) { return "nothing resident" }
        } else {
            $targets = ($rest | ForEach-Object { Resolve-Model $_ })
        }
        foreach ($t in $targets) {
            $b = @{ model = $t; keep_alive = 0 } | ConvertTo-Json
            try {
                Invoke-RestMethod "$OLLAMA/api/generate" -Method Post -Body $b -ContentType "application/json" -TimeoutSec 60 | Out-Null
                "unloaded $t"
            } catch { "FAILED to unload $t -- $($_.Exception.Message)" }
        }
        $v = Get-Vram
        "VRAM now $($v.UsedMB)/$($v.TotalMB) MiB"
    }

    "fit" {
        # weights-only arithmetic: does this set leave room for KV cache?
        $v = Get-Vram
        $inst = Get-Installed
        # NB: keep 'else' on the same line as the closing brace -- a newline
        # between them silently yields $null here instead of the else branch.
        if ($rest.Count -ge 1) {
            $sel = @($rest | ForEach-Object { Resolve-Model $_ })
        } else {
            $sel = @((Get-WesConfig).Values | ForEach-Object { Resolve-Model $_ }) | Select-Object -Unique
        }
        $sum = 0
        foreach ($n in $sel) {
            $m = $inst | Where-Object { $_.name -eq $n }
            if ($null -eq $m) { "  ?     {0,-18} not installed" -f $n; continue }
            $sum += $m.size
            "  +     {0,-18} {1,5} GB" -f $n, (ToGB $m.size)
        }
        $totalGB = [math]::Round($v.TotalMB / 1024, 1)
        $sumGB = ToGB $sum
        $head = [math]::Round($totalGB - $sumGB, 1)
        ""
        "  weights   $sumGB GB / $totalGB GB   -> $head GB left for KV cache + overhead"
        if ($head -lt 2) { "  VERDICT: WILL NOT FIT -- expect CPU spill (catastrophic tok/s)" }
        elseif ($head -lt 4) { "  VERDICT: tight -- fits, but keep num_ctx small" }
        else { "  VERDICT: comfortable" }
        ""
        "  KV cache is NOT counted above and scales linearly with num_ctx."
        "  Measure it for real: load, then compare 'models status' vram to weights."
    }

    default {
        "unknown '$cmd'. try: status check list load unload fit"
    }
}
