# Allow the Prometheus container (inside WSL) to scrape the Windows-side
# services. RUN ELEVATED -- creating a firewall rule needs admin.
#
#   Right-click PowerShell > Run as administrator, then:
#     & C:\Users\awarm\wes\observability\allow-wsl-scrape.ps1
#
# WHY THIS IS NEEDED. Prometheus and Grafana run in Docker inside WSL, but
# windows_exporter (:9182), nvidia_gpu_exporter (:9835) and wes_server (:8080)
# run on Windows. Traffic from WSL to the host crosses the WSL NAT gateway and
# arrives as INBOUND traffic at the Windows firewall, which blocks it by
# default. The symptom is not a connection refused -- it is a TIMEOUT, and
# Prometheus reports "context deadline exceeded" on three targets at once while
# Grafana itself looks perfectly healthy.
#
# The tell that this is a per-port firewall issue and not a bad gateway address:
# port 11434 (Ollama) is reachable from WSL already, because Open WebUI needed
# it and a rule exists for it. Same gateway, same direction, different port.
#
# AN ALLOW RULE ALONE IS NOT ENOUGH, which cost an hour on 2026-09-02. Windows
# evaluates BLOCK rules before ALLOW rules, and this machine had three enabled
# inbound Block rules -- one per executable -- created by dismissing the
# "Windows Defender Firewall has blocked some features of this app" popup:
#
#   windows_exporter.exe      -> blocked :9182
#   nvidia_gpu_exporter.exe   -> blocked :9835
#   "Python" (E:\miniconda3\python.exe) -> blocked :8080, i.e. wes_server
#
# The Python one almost certainly dates from the E: migration, when the new
# interpreter first listened on a port. With those in place the allow rule is
# correct and completely inert. This script removes them; that does NOT open
# anything by itself, because inbound is default-deny and the allow rule above
# is scoped to the WSL range.
#
# ALSO: run this ELEVATED even to LOOK. Unelevated, Get-NetFirewallRule throws
# "Access is denied" on this machine -- and with -ErrorAction SilentlyContinue
# that reads as "no such rule", which is how the block rules went unnoticed
# while three separate diagnoses were built on top of the wrong answer. `netsh
# advfirewall firewall show rule name=all` reads fine without elevation and is
# the honest way to inspect from a normal shell.
#
# HISTORY: docs/observability.md describes a rule "WES exporters (Prometheus
# scrape from Pi)" scoped to 10.0.0.79 for the Raspberry Pi's LAN scrapes. It is
# still present (Private profile, 9182+9835) and is now dead weight -- harmless,
# and left alone by this script since removing it is not ours to decide.
#
# ASCII ONLY in this file: Windows PowerShell 5.1 reads a .ps1 as ANSI unless it
# has a BOM, so a UTF-8 dash inside a double-quoted string terminates the string
# and the file stops parsing. Same rule as pc/scripts/deploy.ps1.
[CmdletBinding()]
param(
    # The address range allowed to reach these ports.
    #
    # Defaults to the whole private /12 rather than the exact WSL subnet
    # (172.22.64.0/20 today) ON PURPOSE: WSL regenerates its subnet, exactly as
    # it regenerates the gateway address that WES_WINDOWS_HOST tracks. A rule
    # pinned to today's /20 would keep working until some future reboot and then
    # fail in a way that looks identical to the problem it fixed.
    #
    # This is not a meaningful widening: 172.16.0.0/12 is private and not
    # routable from outside this machine, and the LAN here is 10.0.0.x, so no
    # other host on the network gains access either way. Pass -RemoteAddress
    # 172.22.64.0/20 if you would rather keep it exact and revisit it later.
    [string]$RemoteAddress = "172.16.0.0/12",
    [int[]]$Ports = @(8080, 9182, 9835),
    # Extra executables to clear Block rules for, on top of whatever is found
    # listening on $Ports. The fallback list matters when a service happens to
    # be down while this runs, since then there is no listener to discover.
    #
    # E:\miniconda3\python.exe, not the venv's python.exe: the venv interpreter
    # is a shim that execs the base one, and the BASE is what actually binds
    # :8080. Its auto-created Block rule is named just "Python", which is why it
    # is so easy to miss.
    [string[]]$AlsoClear = @(
        "E:\miniconda3\python.exe"
        "C:\Users\awarm\wes-pc\.venv\Scripts\python.exe"
    ),
    [switch]$Remove
)
$ErrorActionPreference = "Stop"
$name = "WES metrics (Prometheus scrape from WSL)"

# Log everything to a fixed path. An elevated run happens in a SEPARATE window
# that closes on exit, so without this the outcome -- including the reason for a
# failure -- is visible for about a second and then gone. The first two attempts
# at this rule failed exactly that way. Read it afterwards with:
#   Get-Content C:\Users\awarm\wes-pc\logs\allow-wsl-scrape.log
$logDir = "C:\Users\awarm\wes-pc\logs"
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Force -Path $logDir | Out-Null }
$log = Join-Path $logDir "allow-wsl-scrape.log"
try { Start-Transcript -Path $log -Force | Out-Null } catch { }
"run at {0} by {1}" -f (Get-Date), [Security.Principal.WindowsIdentity]::GetCurrent().Name

# Whatever happens below -- success, a thrown error, or the admin guard -- the
# transcript is closed so the log is complete and readable.
trap {
    Write-Host ("FAILED: " + $_.Exception.Message) -ForegroundColor Red
    try { Stop-Transcript | Out-Null } catch { }
    exit 1
}

$admin = ([Security.Principal.WindowsPrincipal] `
          [Security.Principal.WindowsIdentity]::GetCurrent()
         ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $admin) {
    Write-Host "This script must run ELEVATED (firewall rules need admin)." -ForegroundColor Red
    Write-Host "Open PowerShell as administrator and re-run:"
    Write-Host "  & $PSCommandPath"
    try { Stop-Transcript | Out-Null } catch { }
    exit 1
}

# Idempotent: drop any previous copy so re-running after changing -RemoteAddress
# updates the rule instead of silently stacking a second one beside it.
$existing = Get-NetFirewallRule -DisplayName $name -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "Removing existing rule..."
    $existing | Remove-NetFirewallRule
}
if ($Remove) {
    Write-Host "Removed. WSL can no longer scrape the Windows services."
    try { Stop-Transcript | Out-Null } catch { }
    exit 0
}

New-NetFirewallRule -DisplayName $name -Direction Inbound -Action Allow `
    -Protocol TCP -LocalPort $Ports -RemoteAddress $RemoteAddress `
    -Profile Any -Description ("Lets the Prometheus container in WSL reach " +
        "windows_exporter, nvidia_gpu_exporter and wes_server. See " +
        "observability/allow-wsl-scrape.ps1 and docs/observability.md.") | Out-Null

# Clear the per-executable BLOCK rules that would override the allow above.
# Matched by PROGRAM PATH, never by rule name: these are auto-generated and
# their names are whatever Windows felt like ("Python", "windows_exporter.exe"),
# so a name match would be both unreliable and dangerously broad.
#
# The list is DISCOVERED from whatever is currently listening on $Ports, so it
# tracks reality instead of a hardcoded path that silently rots when the venv
# moves (which is what put "Python" at E:\miniconda3 in the first place).
$blockedPrograms = @()
foreach ($p in $Ports) {
    $conn = Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction SilentlyContinue |
            Select-Object -First 1
    if ($conn) {
        $proc = Get-Process -Id $conn.OwningProcess -ErrorAction SilentlyContinue
        if ($proc -and $proc.Path) {
            "  port {0} is served by {1}" -f $p, $proc.Path
            $blockedPrograms += $proc.Path
        }
    } else {
        "  port {0}: nothing listening right now (using the fallback list)" -f $p
    }
}
$blockedPrograms = ($blockedPrograms + $AlsoClear) | Sort-Object -Unique
Write-Host ""
Write-Host "Checking for per-executable Block rules that would override it..."
$removed = 0
foreach ($prog in $blockedPrograms) {
    Get-NetFirewallApplicationFilter -ErrorAction SilentlyContinue |
        Where-Object { $_.Program -and ($_.Program -ieq $prog) } |
        ForEach-Object {
            $rule = $_ | Get-NetFirewallRule -ErrorAction SilentlyContinue
            if ($rule -and $rule.Direction -eq 'Inbound' -and $rule.Action -eq 'Block') {
                "  removing BLOCK '{0}' -> {1}" -f $rule.DisplayName, $prog
                $rule | Remove-NetFirewallRule
                $script:removed++
            }
        }
}
if ($removed -eq 0) {
    Write-Host "  none found (good)"
} else {
    Write-Host ("  removed {0}. Windows evaluates Block before Allow, so these" -f $removed)
    Write-Host "  made the allow rule inert."
}

$r  = Get-NetFirewallRule -DisplayName $name
$af = $r | Get-NetFirewallAddressFilter
$pf = $r | Get-NetFirewallPortFilter
Write-Host ""
Write-Host "Created:" -ForegroundColor Green
"  name    {0}" -f $r.DisplayName
"  action  {0}   enabled={1}" -f $r.Action, $r.Enabled
"  ports   TCP {0}" -f ($pf.LocalPort -join ",")
"  remote  {0}" -f ($af.RemoteAddress -join ",")
Write-Host ""
# Single-quoted: this hint contains both double quotes and '<', and PowerShell
# has no backslash escape -- a \" inside a double-quoted string ends the string
# and the rest parses as code ("The '<' operator is reserved for future use").
Write-Host 'Verify from WSL (should print OPEN three times):'
Write-Host '  wes-dev.ps1 obs ps   # containers, then:'
Write-Host '  curl.exe -s http://127.0.0.1:9090/api/v1/targets | findstr /C:"health"'
Write-Host 'Or just open the targets page: http://127.0.0.1:9090/targets'
Write-Host ""
Write-Host "To undo: & $PSCommandPath -Remove"
try { Stop-Transcript | Out-Null } catch { }
