# Observability — Prometheus + Grafana

Live and historical utilization graphs, viewable from any LAN browser. Built
2026-07-05 on the Raspberry Pi; **moved onto the PC in Docker on 2026-09-02**
when that hardware was repurposed (`archive/pi/README.md`).

**Dashboard: <http://localhost:3001> → WES → "WES Overview"** (login `admin` /
`admin` unless `observability/.env` says otherwise; LAN-only, no port forwarding).

> Ports below are mirrored from **`hosts.yaml`** (the repo-root host registry,
> read via `wes_hosts.py`) — the single source of truth. If they change, edit
> there. Jarvis can recite the layout via the `lookup_hosts` tool.

## Architecture

Everything is on the PC. The stack is `observability/docker-compose.yml`, which
is also where the operational detail lives — read it before changing anything.

| What | Port | Runs as |
|------|------|---------|
| Prometheus (scrapes + stores, 15s interval, 15d retention) | 9090 (loopback only) | `prom/prometheus` container |
| Grafana (graphs) | 3001 | `grafana/grafana` container |
| windows_exporter v0.31.7 (PC CPU/RAM/disk/net) | 9182 | "WES Exporters" scheduled task |
| nvidia_gpu_exporter v1.9.1 (GPU util/VRAM/temp/power via nvidia-smi) | 9835 | same task |
| `wes_server` `/metrics` (token/call counters by model/source/channel) | 8080 | "WES Server" task |

- **Grafana is on 3001, not 3000.** Open WebUI took 3000 on this machine during
  the September 2026 E: migration, after it had spent a while silently shadowing
  the WES server on 8080. Two services quietly answering on each other's ports
  is the single most confusing failure this system has produced; the ports are
  now written down in `hosts.yaml` and asserted by a unit test.
- **The whole config is in the repo now.** On the Pi, half of it was in
  `/etc/prometheus/` and `/etc/grafana/provisioning/` — hand-created, unversioned,
  and only inspectable by SSHing in. `observability/` now holds the scrape
  config, the alert rules, the datasource and dashboard provisioning, and the
  dashboard JSON.
- PC exporter binaries: `C:\Users\awarm\wes-pc\bin\`; launcher
  `pc/scripts/run_exporters.ps1` (deployed to `C:\Users\awarm\wes-pc\`).
  windows_exporter runs a trimmed collector set
  (`cpu,memory,os,logical_disk,net,thermalzone`); the old `cs` collector was
  removed in v0.31 (its metrics moved into `os`).

### Windows Firewall: run `observability/allow-wsl-scrape.ps1` once

Prometheus is now *inside WSL* and the exporters are on *Windows*, so every
scrape arrives as inbound traffic at the Windows firewall and is dropped by
default. **This bit the very first run of the moved stack** (2026-09-02): both
containers healthy, Grafana serving, and three targets down at once with
`context deadline exceeded`.

Two things make it identifiable:

- **A timeout, not a connection refused.** Refused means nothing is listening;
  a timeout means something ate the packet. The exporters were listening on
  `0.0.0.0` the whole time.
- **Port 11434 (Ollama) was reachable from WSL and these were not** — same
  gateway, same direction, different port. That rules out a wrong
  `WES_WINDOWS_HOST` and points squarely at per-port rules.

Fix, once, elevated:

```powershell
Start-Process powershell -Verb RunAs -ArgumentList '-NoProfile','-NoExit',
  '-File','C:\Users\awarm\wes\observability\allow-wsl-scrape.ps1'
```

It is idempotent, logs to `wes-pc\logs\allow-wsl-scrape.log`, and has a
`-Remove` switch. It allows TCP 8080/9182/9835 from `172.16.0.0/12` rather than
the exact WSL subnet, because WSL regenerates its subnet just as it regenerates
the gateway — a rule pinned to today's `172.22.64.0/20` would work until a
future reboot and then fail in a way that looks exactly like the problem it
fixed. That range is private and unroutable and the LAN here is `10.0.0.x`, so
no other host gains access; the script's header argues the tradeoff and takes
`-RemoteAddress` if you want it exact.

#### An allow rule alone is not enough — clear the per-exe Block rules

**Windows evaluates Block before Allow.** This machine had three enabled
inbound Block rules, one per executable, created by dismissing the "Windows
Defender Firewall has blocked some features of this app" popup:

| Rule name | Program | Silently blocked |
|---|---|---|
| `windows_exporter.exe` | `wes-pc\bin\windows_exporter.exe` | :9182 |
| `nvidia_gpu_exporter.exe` | `wes-pc\bin\nvidia_gpu_exporter.exe` | :9835 |
| **`Python`** | `E:\miniconda3\python.exe` | **:8080, i.e. wes_server** |

With those present the allow rule is correct and completely inert. The script
removes them, matching on **program path, never rule name** — the names are
whatever Windows chose, and "Python" is both uninformative and far too broad to
match on. Removing a Block rule opens nothing by itself: inbound is default-deny
and the allow rule is scoped to the WSL range.

The `Python` entry is the one that hides. `E:\miniconda3\python.exe` is what
actually binds :8080 — the venv's `python.exe` is a shim that execs the base
interpreter — and it almost certainly dates from the E: migration, when that
new interpreter first opened a port.

#### Inspect with `netsh`, not the cmdlets

> **`Get-NetFirewallRule` throws `Access is denied` unelevated on this machine**,
> and with `-ErrorAction SilentlyContinue` that is indistinguishable from "no
> such rule". On 2026-09-02 three separate wrong diagnoses were built on that
> false negative — including a confident claim in this very document that the
> rules below did not exist. They did.
>
> From a normal shell, read the firewall with netsh, which works unelevated:
>
> ```powershell
> netsh advfirewall firewall show rule name=all verbose
> netsh advfirewall firewall show rule name="WES metrics (Prometheus scrape from WSL)"
> ```

For the record, the rule this document has always described —
*"WES exporters (Prometheus scrape from Pi)"*, Private profile, TCP 9182+9835
from `10.0.0.79/32` — **is still present**. It is dead weight now that the Pi is
gone, and harmless; the script deliberately leaves it alone rather than deciding
that for you.

### The one fragile piece: `windows-host`

Docker runs **inside WSL** here, and the exporters run on **Windows**, so every
scrape crosses the WSL NAT gateway. Windows 10 has no mirrored networking, so
that gateway address is regenerated when WSL restarts. Compose maps the name
`windows-host` to it (`WES_WINDOWS_HOST` in `observability/.env`, default
`172.22.64.1`) and everything else refers to the name.

**Symptom when it moves:** every PC panel goes blank at once while Grafana
itself is perfectly healthy, and `TargetDown` fires for `pc_windows`, `pc_gpu`
and `wes_server` together. **Recovery:**

```powershell
wsl -e bash -lc 'ip route show default'      # -> default via 172.22.64.1 ...
# put that in observability/.env, then:
& C:\Users\awarm\wes-pc\wes-dev.ps1 obs "up -d"
```

Three services failing simultaneously is the tell — a real outage takes one down
at a time. On Docker Desktop, set `WES_WINDOWS_HOST=host.docker.internal` and it
stops moving.

## Running it

```powershell
& C:\Users\awarm\wes-pc\wes-dev.ps1 obs ps               # is it up
& C:\Users\awarm\wes-pc\wes-dev.ps1 obs "up -d"          # start / apply changes
& C:\Users\awarm\wes-pc\wes-dev.ps1 obs logs
& C:\Users\awarm\wes-pc\wes-dev.ps1 obs "restart grafana"   # after editing dashboards/
```

That helper is a wrapper: Docker lives in WSL and needs `sudo`, so the raw form
is `wsl -e bash -lc 'cd /mnt/c/Users/awarm/wes/observability && sudo docker compose ...'`.

Prometheus config and alert rules reload without dropping the time series:

```powershell
curl.exe -X POST http://127.0.0.1:9090/-/reload
```

Target health: <http://127.0.0.1:9090/targets>. Alert state:
<http://127.0.0.1:9090/alerts>.

Gotcha (bitten 2026-07-05): scheduled-task PowerShell actions **must include
`-WindowStyle Hidden`** — without it the task runs in a visible console, and
closing that window kills the service with exit 0xC000013A, which does NOT
trigger the task's auto-restart (it's a normal termination, not a failure).
Keep it that way for new ones.

## Dashboard provisioning

The dashboard JSON is versioned at `observability/dashboards/wes-overview.json`
and file-provisioned into the container, so it is **read-only in the UI**. To
change it: edit the JSON, then `wes-dev.ps1 obs "restart grafana"`. (Ad-hoc
dashboards created in the UI still work; they just aren't versioned.)

Datasource UIDs are load-bearing — the dashboard refers to `prom` and `infinity`
by uid, so renaming either in
`observability/grafana/provisioning/datasources/datasources.yml` silently blanks
every panel that uses it.

Live vs cumulative: gauges (CPU%, VRAM, temp) graph directly; for counters use
`rate(x[1m])` for live rates and `increase(x[$__range])` for cumulative totals
over the visible time range.

## WES app metrics (phase 3a — built 2026-07-05)

`GET /metrics` on the server (`prometheus_client`, pinned in the wes-pc venv)
exposes `wes_llm_tokens_total{direction,model,source,channel}` and
`wes_llm_calls_total{model,source,channel}`, incremented in `record_usage()` —
the exact same events as the CSV ledger. Counters reset when the server
restarts; the dashboard uses `rate()`/`increase()` which absorb resets, and
`GET /usage` (backed by the CSV) remains the all-time source of truth. The
"WES tokens" dashboard row prices local tokens at Haiku rates ($1/$5 per MTok,
cached 2026-06-24) — if `wes_server.py`'s pricing constants change, update the
"Est. $ saved" panel expression too.

## Turn log — recent queries/replies/tools (built 2026-07-05)

The dashboard's "Recent turns" table shows the last exchanges verbatim: query,
reply, which tools ran, escalated y/n, per channel. Pipeline:

- `wes_server` logs one JSONL record per exchange to
  `C:\Users\awarm\wes-pc\logs\turns.jsonl` (env `WES_TURNS_LOG`), from the
  `record_turn()` choke point — every channel flows through it.
- **Every REQUEST is logged, success or not** (2026-07-21): logging is decoupled
  from memory, so a turn with an empty/failed reply is still recorded — tagged
  with an `error` field (`"empty_reply"`, or the exception on a crash) so failed
  turns are visible in `/turns` and the table. Only a *non-empty* reply becomes
  conversation memory. (Before this, an empty reply — the "(no reply)" bug — was
  invisible in the log, which is exactly when you most need to see it. The
  request handlers also wrap `think()` so a crash logs the turn + returns a
  graceful reply instead of a silent 500.)
- **This file stores content**, so unlike `usage.csv` it is a rolling window,
  not append-forever: past ~4MB it trims itself to the last `WES_TURNS_MAX`
  (default 2000) exchanges.
- `GET /turns?n=20&channel=discord` serves the tail, newest first.
- Grafana renders it via the **Infinity datasource** plugin
  (`yesoreyeram-infinity-datasource`), installed by `GF_INSTALL_PLUGINS` at
  container start rather than the manual `grafana cli` step the Pi needed.

### The mDNS saga, now moot

On the Pi this table kept going blank with `lookup DESKTOP-R2PFF9T.local on
75.75.75.75:53: no such host`, while Prometheus scraped the identical name fine
and `curl` from the same box returned 200 in 31ms. Cause: Grafana ships
**statically linked**, so it has no cgo resolver, never consults NSS, and falls
back to Go's built-in DNS client — which does not speak mDNS. Prometheus is
dynamically linked and could. That asymmetry was the whole bug, and it needed a
`wes-mdns-hosts` systemd timer on the Pi to seed `/etc/hosts` from `hosts.yaml`
every five minutes (the PC's DHCP lease flaps, so a static entry would have
re-created the original problem).

None of that survives the move: the datasource now points at `windows-host`, an
`/etc/hosts` alias, which Go's pure resolver reads happily. The timer units are
in `archive/pi/` for the record.

## "Metrics stopped working" was the server being dead for a week (2026-08-28)

**Symptom:** the dashboard's WES panels were empty. Everything else worked.

**Cause.** `wes_server` was not running — nothing on :8080 at all, no Python
process. It had died on 2026-08-21 and never came back, so `/metrics` had no
one to serve it. The Discord bot was down from the same day. The machine had
**not** rebooted (27 days' uptime), so this was not the #032 boot race.

What separated the survivors from the casualties was one setting:

| Task | RestartCount | Outcome |
|---|---|---|
| WES Exporters | 999 | still up after 27 days |
| WES Server | 3 | dead |
| WES Discord | 3 | dead |

All three are logon-triggered and equally exposed to whatever transient killed
them. The two with three retries burned through them and Task Scheduler gave up
**permanently**; the one with 999 restarted and carried on. `RestartCount` is
therefore not a tuning knob here, it is the difference between a blip and a
week-long outage. WES Server is now 999.

> **WES Discord is still at 3.** `Set-ScheduledTask` and `schtasks /change` both
> return *Access is denied* for that task from a normal shell (Server accepted
> the identical call), so it needs an elevated prompt:
> `Get-ScheduledTask 'WES Discord'` → set `.Settings.RestartCount = 999` →
> `Set-ScheduledTask`. Until then it will die permanently on its fourth crash.

**The deeper problem is that nobody noticed for a week.** The alert rules cover
`TargetDown` for scrape targets, and `wes_server` IS a scrape target — so a
`TargetDown` should have fired and been DMed. It was not, because the alert
watcher lives in `wes_discord.py`, **which was down too**. The watchdog and the
thing it watches die together, and they died on the same afternoon.

**Moving Prometheus onto this PC does not fix this**, and it is worth being
explicit about that: Prometheus will happily fire `TargetDown` at a Discord bot
that is not running to receive it. Anything that genuinely closes this gap has
to deliver without the bot — tracked as fix 3 in #032.

Check both in one line:

```powershell
Get-ScheduledTask WES* | % { $i=$_|Get-ScheduledTaskInfo
  "{0,-16} {1,-9} restarts={2}" -f $_.TaskName,$_.State,$_.Settings.RestartCount }
```

## Alerting — Jarvis DMs the owner (built 2026-07-05)

Division of labor: **Prometheus evaluates, the Discord bot delivers.**

- Rules live at `observability/prometheus/wes-alerts.yml`, mounted into the
  container. Current set: `TargetDown` (any scrape target, 5m), `GPUHot`
  (>85°C 5m), `PCDiskLow` and `EDiskLow` (<10% free 30m). Prometheus owns
  thresholds, `for:` durations, and flap suppression.
  - `PiHot`/`PiDiskLow` were removed with the Pi. `EDiskLow` was added in the
    same change: the Ollama model store moved to `E:` in the September migration,
    so that volume filling now stops the local model loading at all — something
    the `C:` rule would never have seen.
- `wes_discord.py` runs an `alert_watch` task: polls Prometheus'
  `GET /api/v1/alerts` every 60s (`WES_PROM_URL`, `WES_ALERT_POLL_S`) and
  notifies the owner on changes. One DM per (rule, instance); a still-firing
  alert never repeats. If Prometheus itself is unreachable 5 polls in a row it
  DMs that too (the watchdog needs a watchdog), and again on recovery.
- **Alerts are phrased by Jarvis, not sent raw.** On a change the bot builds a
  grounded event string — the rule's own `summary`, the affected target, and a
  plain-English description of what the rule means (`ALERT_CONTEXT` in
  `wes_discord.py`, keep it in sync with the rules) — and POSTs it to the
  server's **`POST /announce`**. The server has Jarvis explain it in his own
  words AND **records it into the "discord" conversation memory**, so a reply
  like "what was that?" has context (without `/announce` the DM would bypass
  the server and Jarvis would have no memory of sending it). If the server is
  unreachable (which may be *why* an alert fired) the bot falls back to a raw
  `🚨 WES alert: <summary>` DM — an alert is never lost.
- `/announce` (`{"event", "channel"}` → `{"reply"}`) is the general proactive-
  notification primitive; scheduled/deferred actions will reuse it.
- No Alertmanager: for one owner and four rules, the bot-as-receiver keeps it
  to zero new services. Revisit if rules need routing/grouping/silences.
- Verified live 2026-07-05: stopped the PC exporters → two TargetDown DMs
  after 5m → restarted → two Resolved DMs.
- **Encoding gotcha:** under the scheduled task, Python's stdout is cp1252, so
  `print()`-ing an emoji or any non-Latin-1 char raises `UnicodeEncodeError`
  mid-handler. The first real alert died on its own 🚨 this way. Both
  `wes_discord.py` and `wes_server.py` now `reconfigure(errors="replace")`
  their streams in `main`, and the alert watcher logs with `!a` (ASCII repr).
  Keep any new console print in the services ASCII-safe.

## Still planned (phase 3b)

- Turn-latency histograms by channel on `/metrics` (#006). The metric to
  histogram is now `llm_ms` — the stt/ttfa/total split described a pipeline that
  no longer exists.
