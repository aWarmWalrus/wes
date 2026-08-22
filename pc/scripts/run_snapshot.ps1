# WES snapshot refresh - run by the "WES Snapshot" scheduled task (#040).
#
# DAILY at 05:40, twenty minutes before the Fantasy GM cycle at 06:00, so that
# job values players off a board built this morning rather than yesterday's.
#
# ASCII ONLY IN THIS FILE. Windows PowerShell 5.1 reads a .ps1 as ANSI unless
# it has a BOM, so a UTF-8 em-dash inside a double-quoted string terminates the
# string and the file stops parsing. See deploy.ps1 for the full note.
#
# Log-only, like run_fantasy_gm.ps1: WES never speaks on its own initiative
# from a scheduled job. A rejected refresh is marked REJECTED so it is
# grep-able, and the previous snapshot stays in place.
$base = "C:\Users\awarm\wes-pc"
$repo = if ($env:WES_REPO) { $env:WES_REPO } else { "C:\Users\awarm\wes" }
$py = "$base\.venv\Scripts\python.exe"
$logdir = "$base\logs"
New-Item -ItemType Directory -Force -Path $logdir | Out-Null

# UTF-8 end to end. Tee-Object writes UTF-16LE, which makes the log ungreppable
# (see run_sleeper_draft.ps1 for the full story).
$OutputEncoding = New-Object System.Text.UTF8Encoding $false
$env:PYTHONIOENCODING = "utf-8"
$utf8 = New-Object System.Text.UTF8Encoding $false

$stamp = Get-Date -Format "yyyy-MM-dd HH:mm"
$log = "$logdir\snapshot.log"

& $py "$repo\pc\snapshot_refresh.py" 2>&1 | ForEach-Object {
    [System.IO.File]::AppendAllText($log, "$_" + [Environment]::NewLine, $utf8)
}
$result = if ($LASTEXITCODE -eq 0) { "ok" } else { "REJECTED or FAILED" }
[System.IO.File]::AppendAllText($log,
    "$stamp  $result" + [Environment]::NewLine, $utf8)
exit $LASTEXITCODE
