# WES nightly eval — run by the "WES Nightly Eval" scheduled task (see
# docs/eval-design.md section 7). Runs the latency gate (perf_check) and the
# quality eval (eval_turns with the FREE local gemma4:12b judge — no API cost,
# no key dependency) against the live server, then appends a one-line verdict
# to logs\eval.log. Full output of the most recent run: logs\eval_last.log
# (UTF-16, like server.log). Log-only by design: WES never speaks on its own
# initiative (house rule) — red in eval.log means look, green means ignore.
$base = "C:\Users\awarm\wes-pc"
$py = "$base\.venv\Scripts\python.exe"
$logdir = "$base\logs"
New-Item -ItemType Directory -Force -Path $logdir | Out-Null
$detail = "$logdir\eval_last.log"
$stamp = Get-Date -Format "yyyy-MM-dd HH:mm"

Set-Content $detail "==== WES nightly eval $stamp ===="
& $py Z:\wes\tests\perf_check.py *>> $detail
$perf = if ($LASTEXITCODE -eq 0) { "perf OK" } else { "perf FAIL" }
# Web-search golden cases hit the PAID Anthropic web-search API, so run them
# only ONCE A WEEK (Sunday), not every night. --web-search opts them in; every
# other night they're skipped and the run stays free.
$evalArgs = @('Z:\wes\tests\eval_turns.py', '--judge', 'local')
$web = ''
if ((Get-Date).DayOfWeek -eq 'Sunday') { $evalArgs += '--web-search'; $web = " +web" }
& $py @evalArgs *>> $detail
$eval = if ($LASTEXITCODE -eq 0) { "eval OK$web" } else { "eval FAIL$web" }
Add-Content "$logdir\eval.log" "$stamp  $perf  $eval"
