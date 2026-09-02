# WES Fantasy GM cycle — run by the "WES Fantasy GM" scheduled task (ticket
# #029 P3/P4). Runs propose_lineup_change for every propose/auto team, logging
# one line per team to logs\fantasy_gm.log. Log-only by design here — same
# house rule as run_nightly_eval.ps1: WES never speaks on its own initiative
# from THIS script, so a routine "already optimal" run stays silent. A REAL
# WRITE is marked [EXECUTED] in the log so it's grep-able, AND the owner gets
# DMed about it separately: the always-running "WES Discord" bot's
# fantasy_watch (pc/wes_discord.py) polls the same ledger this run writes to
# and sends the notification — decoupled from this script on purpose, so the
# scheduled task stays a dumb, log-only batch job and the notification logic
# lives in one place regardless of what triggered the write.
$base = "C:\Users\awarm\wes-pc"
# Code now runs from a LOCAL clone, not the Z: share: the PC must not need
# the Pi up to start (see #032), and execution policy blocks unsigned
# scripts on a share entirely. WES_REPO overrides for a different checkout.
$repo = if ($env:WES_REPO) { $env:WES_REPO } else { "C:\Users\awarm\wes" }
$py = "$base\.venv\Scripts\python.exe"
$logdir = "$base\logs"
New-Item -ItemType Directory -Force -Path $logdir | Out-Null
$detail = "$logdir\fantasy_gm_last.log"
$stamp = Get-Date -Format "yyyy-MM-dd HH:mm"

# UTF-8 END TO END, for the same reason as run_sleeper_draft.ps1 — this script
# had the bug that one documents. `Set-Content` wrote the header in ANSI and
# `*>>` appended in UTF-16LE, so the log was two encodings in one file: a NUL
# between every character of the Python output, every em-dash mojibake, and
# grep finding nothing. A log you cannot read is not a log, and this one is
# where a failed lineup write gets explained.
$OutputEncoding = New-Object System.Text.UTF8Encoding $false
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false
$env:PYTHONIOENCODING = "utf-8"
$utf8 = New-Object System.Text.UTF8Encoding $false

[System.IO.File]::WriteAllText($detail,
    "==== WES fantasy GM $stamp ====" + [Environment]::NewLine, $utf8)

# -u (UNBUFFERED): piped to a file rather than a console, Python block-buffers
# stdout, and a whole GM cycle fits inside one buffer — so the log would arrive
# only at exit, which is no use while a run is hanging.
& $py -u "$repo\pc\fantasy_gm_run.py" 2>&1 | ForEach-Object {
    $line = "$_"
    Write-Output $line
    [System.IO.File]::AppendAllText($detail, $line + [Environment]::NewLine, $utf8)
}

$result = if ($LASTEXITCODE -eq 0) { "ok" } else { "ERROR (see fantasy_gm_last.log)" }
[System.IO.File]::AppendAllText("$logdir\fantasy_gm.log",
    "$stamp  $result" + [Environment]::NewLine, $utf8)
