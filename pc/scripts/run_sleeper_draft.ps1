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
      [string]$User,
      [ValidateSet('off','propose','auto')][string]$Banter = 'auto',
      [double]$BanterGap = 0)
# -User picks the Sleeper ACCOUNT to draft as; default gmbartimusprime. The
# token follows the name (WES_SLEEPER_TOKEN_<USER>, else the shared one), so
# this switches credentials and seat together rather than half of each.
# -Banter defaults to 'auto' (posts, 2026-08-28). Use -Banter propose in a room
# of strangers: it composes and logs without sending anything.

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
# THE ACCOUNT TOO, for the same reason and a sharper one: the token and the
# username have to match. A shell opened before WES_SLEEPER_USER was set would
# take the built-in default while the token in the environment belongs to a
# different account -- writes land as one person, seat lookups key on another,
# and nothing says so.
if (-not $env:WES_SLEEPER_USER) {
    $env:WES_SLEEPER_USER = [Environment]::GetEnvironmentVariable(
        'WES_SLEEPER_USER', 'User')
}

# BANTER RUNS ON THE SMALL MODEL, picks stay on the 12b.
#
# Measured on 12 real logged payloads (2026-09-03): gemma3:4b answers a chat
# line in 0.73s median against the 12b's 2.03s, produces valid JSON just as
# reliably, and was not caught by the unverifiable() falsehood guard once. It
# takes chat off the 12b entirely, so a chat line can no longer queue ahead of
# a pick -- one Ollama serves this whole machine and it serialises requests.
#
# THE COST IS TONE, and it is visible: the 4b is formulaic ("Ugh, X?
# Seriously?" three times running) and produced one line that read as
# semantically inverted. The falsehood guard catches checkable false claims,
# not clumsy ones. It also costs ~3GB of VRAM, leaving roughly 2GB spare on
# this card once both models are resident.
#
# To put chat back on the 12b, delete this line (the code falls back to
# WES_ESCALATE_MODEL). The pre-flight warms whichever model is configured.
if (-not $env:WES_BANTER_MODEL) { $env:WES_BANTER_MODEL = "gemma3:4b" }

# RUN AS A MODULE, not a script path. The Sleeper code lives in the `sleeper`
# package now, and `python pc\sleeper\draft_day.py` would put pc\sleeper on
# sys.path instead of pc -- so `from sleeper import data` would not resolve.
#
# -P IS LOAD-BEARING, not tidiness. Without it, -m puts the CALLER'S WORKING
# DIRECTORY on sys.path, and from C:\Users\awarm that makes the sleeperdraft
# REPO FOLDER shadow the installed package: it has no __init__.py at the top
# level, so Python takes it as a namespace package and every attribute
# vanishes --
#     AttributeError: module 'sleeperdraft' has no attribute 'join_draft'
# which is precisely how this broke when the package move landed. -P drops CWD
# from sys.path entirely, so the launcher works from any directory; PYTHONPATH
# supplies `sleeper` and site-packages supplies `sleeperdraft`. (Python 3.11+;
# the venv is 3.11.5.)
$env:PYTHONPATH = "$repo\pc"
$args = @("-P", "-m", "sleeper.draft_day", "--wait-hours", "$WaitHours")
if ($Check) { $args += "--check" }
if ($DraftId) { $args += @("--draft", $DraftId) }
# -Join claims a free seat in -DraftId first. A WRITE, so it is opt-in; pair it
# with -Check to get seated and verified without then drafting.
if ($Join) { $args += "--join" }
if ($User) { $args += @("--username", $User) }
if ($League) { $args += @("--league", $League) }
if ($RebuildSnapshot) { $args += "--rebuild-snapshot" }
# ALWAYS PASS IT, including "off". This used to send the flag only when banter
# was on, which was harmless while the script also defaulted to off -- and
# became a trap the moment the script default became auto: -Banter off would
# have sent nothing and silently produced a posting bot.
$args += @("--banter", $Banter)
if ($BanterGap -gt 0) { $args += @("--banter-gap", "$BanterGap") }

# UTF-8 END TO END. Tee-Object writes UTF-16LE, which put a NUL between every
# character of the log: grep found nothing, tail printed spaced-out gibberish,
# and every em-dash arrived as mojibake. A log you cannot grep is not a log.
# Setting the console encoding too keeps the live view readable.
$OutputEncoding = New-Object System.Text.UTF8Encoding $false
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false
$env:PYTHONIOENCODING = "utf-8"

# STOP THE DISCORD BOT FOR THE DURATION, and put it back afterwards.
#
# The bot and the draft share one Ollama, and Ollama serialises PER MODEL. A
# Discord turn on the deep tier (thinking on, 2048-token budget) holds
# gemma4:12b for ~36s, so a pick landing in that window queues behind it --
# measured 2026-09-03 at 2.07s of work and 37.32s of waiting, which accounted
# exactly for the 40s picks in that day's mock. Banter is unaffected: it runs on
# gemma3:4b now, and different models DO run concurrently.
#
# NOT on -Check. A pre-flight must not change the system, and the pre-flight's
# own advisory line reports the bot's state -- auto-stopping would make that
# line a tautology.
#
# ONLY RESTARTED IF WE STOPPED IT. Starting a bot the owner had deliberately
# stopped would be us overriding a decision we cannot see the reason for.
$discordStopped = $false
if (-not $Check) {
    try {
        $dt = Get-ScheduledTask -TaskName "WES Discord" -ErrorAction SilentlyContinue
        if ($dt -and $dt.State -eq "Running") {
            Stop-ScheduledTask -TaskName "WES Discord"
            $discordStopped = $true
            Write-Output ("stopped 'WES Discord' for the draft " +
                          "(alert + fantasy DM watchers are down until it restarts)")
        }
    } catch {
        Write-Output "could not stop 'WES Discord': $($_.Exception.Message)"
    }
}

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
# try/finally, NOT a trailing Start-ScheduledTask. The draft can end by
# crashing, by Ctrl-C, or by the window being closed, and "the bot never came
# back" is a silent outage of the alert watcher -- the exact failure that went
# unnoticed for a week in August. finally runs on the error and Ctrl-C paths;
# only a hard kill escapes it, and that is what the pre-flight advisory is for.
try {
    & $py -u @args 2>&1 | ForEach-Object {
        $line = "$_"
        Write-Output $line
        [System.IO.File]::AppendAllText($log, $line + [Environment]::NewLine, $utf8)
    }
    $code = $LASTEXITCODE
} finally {
    if ($discordStopped) {
        try {
            Start-ScheduledTask -TaskName "WES Discord"
            Write-Output "restarted 'WES Discord'"
        } catch {
            # LOUD. A bot that did not come back monitors nothing and says
            # nothing about it, so this must never be a silent failure.
            Write-Output ("!! FAILED to restart 'WES Discord': " +
                          "$($_.Exception.Message) -- run " +
                          "Start-ScheduledTask -TaskName 'WES Discord'")
        }
    }
}

if ($code -ne 0) {
    Write-Output "draft run exited $code (see $log)"
}
exit $code
