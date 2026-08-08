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
$py = "$base\.venv\Scripts\python.exe"
$logdir = "$base\logs"
New-Item -ItemType Directory -Force -Path $logdir | Out-Null
$detail = "$logdir\fantasy_gm_last.log"
$stamp = Get-Date -Format "yyyy-MM-dd HH:mm"

Set-Content $detail "==== WES fantasy GM $stamp ===="
& $py Z:\wes\pc\fantasy_gm_run.py *>> $detail
$result = if ($LASTEXITCODE -eq 0) { "ok" } else { "ERROR (see fantasy_gm_last.log)" }
Add-Content "$logdir\fantasy_gm.log" "$stamp  $result"
