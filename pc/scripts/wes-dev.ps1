# WES dev helper — one stable entrypoint for the whole dev loop, so it can be
# allowlisted with a SINGLE rule (PowerShell(& ...wes-dev.ps1*)) instead of a
# fresh permission prompt for every bespoke command.
#
#   & C:\Users\awarm\wes-pc\wes-dev.ps1 <command> [args]
#
# commands:
#   test [file|node] [args] run the unit suite; a leading path runs ONLY that
#                           (test_unit_sleeper.py resolves against tests\)
#   eval [local|haiku]      run the golden eval (judge defaults to local)
#   perf                    run perf_check.py
#   reload [server|discord|exporters]   restart the task (default server) + wait health
#   health                  GET /health
#   say <channel> <text>    POST /respond_text (text-only; no audio) -> prints reply
#   reset [channel]         POST /reset_conversation (all channels if omitted)
#   turns [n]               GET /turns
#   usage                   GET /usage
#   log [server|discord|exporters] [n]   tail a service log (null-stripped)
#   gpu                     nvidia-smi + ollama ps
#   models [status|check|list|load|unload|fit]   VRAM/model manager (wes-models.ps1)
#   deploy [check]          re-copy launcher scripts from the repo to local
#                           ("check" reports drift without changing anything)
#   sync                    pull BOTH clones (PC + Pi), redeploy, compare HEADs
#
# THIS FILE LIVES IN THE REPO (pc/scripts/) and is DEPLOYED to the path above.
# Edit it here; run `deploy` to push it out. Editing the deployed copy works
# until the next deploy silently overwrites it; `deploy -Check` catches that.
param(
    [Parameter(Mandatory = $true)][string]$cmd,
    [Parameter(ValueFromRemainingArguments = $true)]$rest
)
$ErrorActionPreference = "Stop"
$py   = "C:\Users\awarm\wes-pc\.venv\Scripts\python.exe"
$base = "C:\Users\awarm\wes-pc"
# Code now runs from a LOCAL clone, not the Z: share: the PC must not need
# the Pi up to start (see #032), and execution policy blocks unsigned
# scripts on a share entirely. WES_REPO overrides for a different checkout.
$repo = if ($env:WES_REPO) { $env:WES_REPO } else { "C:\Users\awarm\wes" }
$srv  = "http://127.0.0.1:8080"
if ($null -eq $rest) { $rest = @() }

function Tail-Log($name, $n) {
    $p = "$base\logs\$name.log"
    if (-not (Test-Path $p)) { return "no log at $p" }
    (( Get-Content $p -Raw ) -replace "`0", "") -split "`n" | Select-Object -Last $n
}
function Wait-Health {
    $end = (Get-Date).AddSeconds(150)
    do {
        try { return (Invoke-RestMethod "$srv/health" -TimeoutSec 3) }
        catch { Start-Sleep 4 }
    } while ((Get-Date) -lt $end)
    return "server did not answer /health within 150s"
}

switch ($cmd) {
    "test"   {
        # TARGETING ACTUALLY TARGETS. This used to pass the whole tests
        # directory AND then append whatever you asked for, so
        # "wes-dev test test_unit_sleeper.py" ran all 943 tests plus that file
        # again - 15s instead of 2.5s, and it looked like the suite was simply
        # slow. It is not: one file is 2.5s and nearly all of that is pytest
        # starting up.
        #
        # A leading PATH (or a node id containing ::) means "run only this".
        # Anything else - -k, -x, --durations - still applies to the whole
        # suite, so plain flags behave exactly as before.
        $first = if ($rest.Count -ge 1) { [string]$rest[0] } else { "" }
        $target = ""
        if ($first -and -not $first.StartsWith("-")) {
            foreach ($cand in @($first, "$repo\tests\$first")) {
                if ((Test-Path $cand) -or $cand.Contains("::")) { $target = $cand; break }
            }
        }
        if ($target) {
            $extra = if ($rest.Count -ge 2) { @($rest | Select-Object -Skip 1) } else { @() }
            & $py -m pytest $target -q @extra
        } else {
            & $py -m pytest "$repo\tests" -q --ignore="$repo\tests\test_faces.py" @rest
        }
    }
    "eval"   {
        # First arg is the judge IF it names one; anything else forwards to
        # eval_turns.py. So all of these route through this ONE allowlisted verb:
        #   eval            eval haiku      eval local --web-search
        #   eval --only web-weather         eval both --no-history
        $judges = @('haiku', 'local', 'both', 'off')
        if ($rest.Count -ge 1 -and $judges -contains $rest[0]) {
            $judge = $rest[0]
            $extra = if ($rest.Count -ge 2) { @($rest | Select-Object -Skip 1) } else { @() }
        } else {
            $judge = 'local'
            $extra = @($rest)
        }
        & $py "$repo\tests\eval_turns.py" --judge $judge @extra
    }
    "perf"   { & $py "$repo\tests\perf_check.py" }
    "reload" {
        $task = switch ($rest[0]) {
            "discord"   { "WES Discord" }
            "exporters" { "WES Exporters" }
            default     { "WES Server" }
        }
        Stop-ScheduledTask -TaskName $task; Start-Sleep 2; Start-ScheduledTask -TaskName $task
        "restarted '$task'"
        if ($task -eq "WES Server") { Wait-Health | Out-Null; "server /health is up" }
    }
    "health" { Invoke-RestMethod "$srv/health" | ConvertTo-Json -Depth 4 }
    "say"    {
        if ($rest.Count -lt 2) { return "usage: say <channel> <text>" }
        $body = @{ channel = $rest[0]; text = (($rest | Select-Object -Skip 1) -join " ") } | ConvertTo-Json
        (Invoke-RestMethod "$srv/respond_text" -Method Post -ContentType "application/json" -Body $body).reply
    }
    "reset"  {
        $body = if ($rest.Count -ge 1) { @{ channel = $rest[0] } | ConvertTo-Json } else { "{}" }
        Invoke-RestMethod "$srv/reset_conversation" -Method Post -ContentType "application/json" -Body $body
    }
    "turns"  {
        $n = if ($rest.Count -ge 1) { $rest[0] } else { 15 }
        (Invoke-RestMethod "$srv/turns?n=$n").turns | ConvertTo-Json -Depth 4
    }
    "usage"  { Invoke-RestMethod "$srv/usage" | ConvertTo-Json -Depth 5 }
    "log"    {
        $name = if ($rest.Count -ge 1) { $rest[0] } else { "server" }
        $n    = if ($rest.Count -ge 2) { [int]$rest[1] } else { 30 }
        Tail-Log $name $n
    }
    "gpu"    {
        nvidia-smi --query-gpu=memory.used,memory.total,utilization.gpu --format=csv,noheader
        ollama ps
    }
    "models" {
        # NB: '$x = if (..) {..} else { @() }' yields $null, not an empty array,
        # and splatting @$null passes one NULL positional arg. Assign the array
        # separately and bind -rest by name instead of splatting.
        $sub = if ($rest.Count -ge 1) { [string]$rest[0] } else { "status" }
        $margs = @()
        if ($rest.Count -ge 2) { $margs = @($rest | Select-Object -Skip 1) }
        & "$base\wes-models.ps1" -cmd $sub -rest $margs
    }
    "deploy" {
        # The LOCAL copy, not the repo one: execution policy refuses to run an
        # unsigned script off the Z: share. deploy.ps1 reads its sources from
        # the repo, so the repo is still the source of truth.
        #
        # Takes the plain word 'check', not -Check: a leading dash gets bound as
        # a parameter of THIS script before it can reach $rest.
        #
        # And the switch is passed EXPLICITLY, not splatted. Splatting an array
        # (@('-Check')) passes its elements POSITIONALLY, so deploy.ps1 -- which
        # has no positional parameters -- rejects it with the same
        # "positional parameter cannot be found" error. Splatting only binds by
        # name when the splatted variable is a hashtable.
        $checkOnly = ($rest.Count -ge 1 -and
                      @('check', '-check', '--check') -contains [string]$rest[0])
        if ($checkOnly) { & "$base\deploy.ps1" -Check }
        else            { & "$base\deploy.ps1" }
    }
    "sync" {
        # Two clones now exist (PC local + the Pi's), so they can diverge. This
        # is the routine that stops that being silent: pull both, redeploy the
        # launchers, and print both HEADs so a mismatch is visible.
        #
        # --ff-only on purpose: a clone that has drifted should FAIL loudly here
        # rather than be quietly merged by a helper script.
        #
        # The Pi's clone is reached through the Z: MOUNT, not over ssh. Running
        # `ssh walrus-pi git pull` fails with "could not read Username for
        # https://github.com" -- the Pi's remote is HTTPS and it has no stored
        # credentials, whereas git on the PC does. Driving the Pi's working tree
        # across the share reuses the PC's credentials and needs no secret on
        # the Pi at all.
        $piRepo = "Z:\wes"
        git -C $repo pull --ff-only
        & "$base\deploy.ps1"
        if (Test-Path $piRepo) {
            git -C $piRepo pull --ff-only
            $piHead = (git -C $piRepo rev-parse --short HEAD)
        } else {
            $piHead = "(Z: not mapped - Pi clone not checked)"
        }
        $pcHead = (git -C $repo rev-parse --short HEAD)
        "PC clone: $pcHead"
        "Pi clone: $piHead"
        if ($pcHead -ne $piHead) {
            "WARNING: clones differ. Push from whichever is ahead, then sync again."
            "         The Pi runs pi/ from ITS clone, so the voice client may be stale."
        }
    }
    default  { "unknown command '$cmd'. try: test eval perf reload health say reset turns usage log gpu models deploy sync" }
}
