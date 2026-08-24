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
# -DraftId targets ANY draft you have joined - a mock, or someone else's draft.
# The seat is read from that draft's own draft_order, because a mock has no
# league at all (league_id is null) and the roster route cannot find you.
# Scoring still comes from -League, since a mock has no scoring of its own.
[CmdletBinding()]
param([switch]$Check, [double]$WaitHours = 8.0, [string]$DraftId,
      [string]$League, [switch]$RebuildSnapshot, [switch]$Join,
      [ValidateSet('off','propose','auto')][string]$Banter = 'off',
      [double]$BanterGap = 0)

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
if ($DraftId) { $args += @("--draft", $DraftId) }
# -Join claims a free seat in -DraftId first. A WRITE, so it is opt-in; pair it
# with -Check to get seated and verified without then drafting.
if ($Join) { $args += "--join" }
if ($League) { $args += @("--league", $League) }
if ($RebuildSnapshot) { $args += "--rebuild-snapshot" }
if ($Banter -ne "off") { $args += @("--banter", $Banter) }
if ($BanterGap -gt 0) { $args += @("--banter-gap", "$BanterGap") }

# UTF-8 END TO END. Tee-Object writes UTF-16LE, which put a NUL between every
# character of the log: grep found nothing, tail printed spaced-out gibberish,
# and every em-dash arrived as mojibake. A log you cannot grep is not a log.
# Setting the console encoding too keeps the live view readable.
$OutputEncoding = New-Object System.Text.UTF8Encoding $false
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false
$env:PYTHONIOENCODING = "utf-8"

$stamp = Get-Date -Format "yyyy-MM-dd HH:mm"
$log = "$logdir\sleeper_draft.log"
$utf8 = New-Object System.Text.UTF8Encoding $false
[System.IO.File]::AppendAllText($log,
    "==== WES sleeper draft $stamp ====" + [Environment]::NewLine, $utf8)

# Stream to the console AND the file, line by line. Tee-Object would be the
# obvious tool; it is the one that broke the encoding.
# -u (UNBUFFERED) is load-bearing, not tidiness. Piped to anything other than
# a console, Python block-buffers stdout, and a pre-flight is well under one
# buffer -- so a backgrounded run wrote its launcher header and then appeared
# to hang for five minutes while it was in fact working perfectly. A log that
# arrives after the draft is not a log.
& $py -u @args 2>&1 | ForEach-Object {
    $line = "$_"
    Write-Output $line
    [System.IO.File]::AppendAllText($log, $line + [Environment]::NewLine, $utf8)
}

if ($LASTEXITCODE -ne 0) {
    Write-Output "draft run exited $LASTEXITCODE (see $log)"
}
exit $LASTEXITCODE
