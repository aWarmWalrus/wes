# WES Sleeper draft day (ticket #039).
#
#   & C:\Users\awarm\wes-pc\run_sleeper_draft.ps1 -Check   # pre-flight only
#   & C:\Users\awarm\wes-pc\run_sleeper_draft.ps1          # wait, then draft
#
# ASCII ONLY IN THIS FILE. Windows PowerShell 5.1 reads a .ps1 as ANSI unless
# it has a BOM, so a UTF-8 em-dash inside a double-quoted string terminates the
# string and the file stops parsing. See deploy.ps1 for the full note.
#
# NOT A SCHEDULED TASK, deliberately. The draft is one event on one afternoon,
# and a task that fires on a timer would either sit idle for weeks or start
# unattended. Start this by hand on the day and watch the log. It waits for the
# room to open on its own; it does NOT start the draft, which is Sleeper's to
# do on the schedule the commissioner set.
[CmdletBinding()]
param([switch]$Check, [double]$WaitHours = 8.0)

$base = "C:\Users\awarm\wes-pc"
$repo = if ($env:WES_REPO) { $env:WES_REPO } else { "C:\Users\awarm\wes" }
$py = "$base\.venv\Scripts\python.exe"
$logdir = "$base\logs"
New-Item -ItemType Directory -Force -Path $logdir | Out-Null

# A shell opened before the token was set does not inherit it. wes_sleeper now
# falls back to the persisted value itself, but passing it explicitly keeps the
# failure impossible rather than merely recovered from.
if (-not $env:WES_SLEEPER_TOKEN) {
    $env:WES_SLEEPER_TOKEN = [Environment]::GetEnvironmentVariable(
        'WES_SLEEPER_TOKEN', 'User')
}

$args = @("$repo\pc\sleeper_draft_day.py", "--wait-hours", "$WaitHours")
if ($Check) { $args += "--check" }

$stamp = Get-Date -Format "yyyy-MM-dd HH:mm"
$log = "$logdir\sleeper_draft.log"
Add-Content $log "==== WES sleeper draft $stamp ===="

# Tee so the owner can watch it live AND have the record afterwards. A draft is
# not something you want to reconstruct from memory.
& $py @args 2>&1 | Tee-Object -FilePath $log -Append

if ($LASTEXITCODE -ne 0) {
    Write-Output "draft run exited $LASTEXITCODE (see $log)"
}
exit $LASTEXITCODE
