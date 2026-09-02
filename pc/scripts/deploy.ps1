# Sync the launcher/helper scripts from the REPO to the PC-local directory that
# the scheduled tasks actually run from.
#
# ASCII ONLY IN THIS FILE, deliberately. Windows PowerShell 5.1 reads a .ps1 as
# ANSI unless it has a BOM, so a UTF-8 em-dash (E2 80 94) decodes as cp1252
# 'a-euro-"'. In a comment that is harmless mojibake; inside a double-quoted
# string the trailing quote TERMINATES THE STRING and the file no longer parses.
# The sibling scripts get away with em-dashes only because theirs are in
# comments. Keep new strings ASCII rather than relying on that.
#
# WHY TWO COPIES EXIST AT ALL, since that looks redundant. The reason is
# historical but the arrangement is still deliberate:
#
# 1. The repo used to live on the Raspberry Pi and reach the PC over SMB as Z:.
#    With the tasks running launchers straight out of Z:, a launcher could not
#    start unless the Pi was up AND the share was mounted, and at boot it
#    frequently was not. That is ticket #032, which took the services down for
#    ~24h while Task Scheduler reported success.
# 2. PowerShell's execution policy refuses to run an unsigned script off a
#    network share at all ("is not digitally signed"), because Z: landed in the
#    remote zone. So even with the Pi up and the share mounted, a launcher on Z:
#    could not execute.
#
# The Pi and the share are both gone (2026-09-02) and the repo is on local disk,
# so neither hazard is live. The split stays because the deployed copy is what
# Task Scheduler holds a path to, and because a `git pull` that half-rewrites a
# launcher while a task is starting is a race worth not having.
#
# The local copies are therefore GENERATED. Edit pc/scripts/*.ps1 in the repo
# and re-run this. Editing a deployed copy works right up until the next deploy
# silently discards it, which is what -Check exists to catch.
#
# This script deploys ITSELF along with the rest, so run the LOCAL copy:
#   & C:\Users\awarm\wes-pc\deploy.ps1            # copy repo -> local
#   & C:\Users\awarm\wes-pc\deploy.ps1 -Check     # report drift, change nothing
# or via the helper:  wes-dev.ps1 deploy [-Check]
#
# Bootstrapping a clean machine is a plain file copy, not a script run:
#   Copy-Item C:\Users\awarm\wes\pc\scripts\*.ps1 C:\Users\awarm\wes-pc\
[CmdletBinding()]
param([switch]$Check)

# The REPO is the source, always - NOT $PSScriptRoot. This script normally runs
# from the deployed copy, so deriving the source from its own location would
# make it copy the local directory onto itself and silently never update.
$src = if ($env:WES_REPO) { "$env:WES_REPO\pc\scripts" } else { "C:\Users\awarm\wes\pc\scripts" }
$dst = if ($env:WES_PC_HOME) { $env:WES_PC_HOME } else { "C:\Users\awarm\wes-pc" }

if (-not (Test-Path $src)) {
    Write-Error "repo scripts not found at '$src'. Is the local clone present? Set WES_REPO to override."
    exit 1
}
if (-not (Test-Path $dst)) {
    Write-Error "destination '$dst' does not exist. Set WES_PC_HOME or create it."
    exit 1
}

$drift = 0
$copied = 0
foreach ($f in Get-ChildItem "$src\*.ps1") {
    $target = Join-Path $dst $f.Name
    $same = $false
    if (Test-Path $target) {
        $same = (Get-FileHash $f.FullName).Hash -eq (Get-FileHash $target).Hash
    }
    if ($same) { continue }

    if ($Check) {
        $state = if (Test-Path $target) { "DIFFERS from repo" } else { "MISSING" }
        Write-Output "  $($f.Name): $state"
        $drift++
    } else {
        # Overwriting the running script (this one) is safe: PowerShell reads
        # the whole file before executing it.
        Copy-Item $f.FullName $target -Force
        Write-Output "  deployed $($f.Name)"
        $copied++
    }
}

if ($Check) {
    if ($drift -eq 0) {
        Write-Output "all launcher scripts match the repo"
    } else {
        Write-Output "$drift script(s) out of sync. Run without -Check to fix."
    }
    exit $drift
}
if ($copied -eq 0) {
    Write-Output "already up to date"
} else {
    Write-Output "$copied script(s) deployed to $dst"
    Write-Output "NOTE: a redeployed launcher takes effect on the next task restart."
}
