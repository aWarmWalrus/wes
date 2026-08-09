# WES nightly eval — run by the "WES Nightly Eval" scheduled task (see
# docs/eval-design.md section 7). Runs the latency gate (perf_check) and the
# quality eval (eval_turns with the FREE local gemma4:12b judge — no API cost,
# no key dependency) against the live server, then appends a one-line verdict
# to logs\eval.log. Full output of the most recent run: logs\eval_last.log
# (UTF-16, like server.log). Log-only by design: WES never speaks on its own
# initiative (house rule) — red in eval.log means look, green means ignore.
$base = "C:\Users\awarm\wes-pc"
# Code now runs from a LOCAL clone, not the Z: share: the PC must not need
# the Pi up to start (see #032), and execution policy blocks unsigned
# scripts on a share entirely. WES_REPO overrides for a different checkout.
$repo = if ($env:WES_REPO) { $env:WES_REPO } else { "C:\Users\awarm\wes" }
$py = "$base\.venv\Scripts\python.exe"
$logdir = "$base\logs"
New-Item -ItemType Directory -Force -Path $logdir | Out-Null
$detail = "$logdir\eval_last.log"
$stamp = Get-Date -Format "yyyy-MM-dd HH:mm"

Set-Content $detail "==== WES nightly eval $stamp ===="
& $py "$repo\tests\perf_check.py" *>> $detail
$perf = if ($LASTEXITCODE -eq 0) { "perf OK" } else { "perf FAIL" }
# Web-search golden cases hit the PAID Anthropic web-search API, so run them
# only ONCE A WEEK (Sunday), not every night. --web-search opts them in; every
# other night they're skipped and the run stays free.
$evalArgs = @("$repo\tests\eval_turns.py", '--judge', 'local')
$web = ''
if ((Get-Date).DayOfWeek -eq 'Sunday') { $evalArgs += '--web-search'; $web = " +web" }
& $py @evalArgs *>> $detail
$eval = if ($LASTEXITCODE -eq 0) { "eval OK$web" } else { "eval FAIL$web" }
Add-Content "$logdir\eval.log" "$stamp  $perf  $eval"
