"""Discord frontend for WES — talk to Jarvis by text when away from home.

A thin bridge, not a pipeline: forwards the owner's Discord messages to the
local server's text endpoint (`POST /respond_text`, channel "discord") and
posts the reply back. No audio path is involved, so the house audio rule is
never in play, and the "discord" conversation channel keeps remote chats from
clobbering the in-house voice context.

Auth is a hard allowlist of one Discord user ID — everyone else is ignored
silently. This process is an internet-facing door into the tool loop, so it
stays read/answer-only by design (the server exposes no action tools yet;
revisit the rails here before it does).

Run (PC, same venv as the server):
    C:\\Users\\awarm\\wes-pc\\.venv\\Scripts\\python.exe Z:\\wes\\pc\\wes_discord.py

Env:
    WES_DISCORD_TOKEN     bot token (Discord developer portal; user env, like
                          the Anthropic key — never the repo)
    WES_DISCORD_OWNER_ID  the one Discord user ID allowed to talk to the bot
    WES_SERVER_URL        default http://127.0.0.1:8080
    WES_PROM_URL          Prometheus for the alert watcher (default the Pi,
                          http://10.0.0.79:9090); WES_ALERT_POLL_S interval
    WES_FANTASY_POLL_S    fantasy-write watcher poll interval (default 60s;
                          see fantasy_watch — DMs on a real Yahoo lineup write)
"""
import asyncio
import json
import os
import sys
import time
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import wes_hosts  # noqa: E402 — host registry (hosts.yaml); repo root on path
import wes_execute  # noqa: E402 — fantasy ledger path + kill switch (#029 P3)

# The server runs on the same PC (loopback); Prometheus is on the Pi.
SERVER_URL = os.environ.get(
    "WES_SERVER_URL",
    f"http://127.0.0.1:{wes_hosts.port('pc', 'server', default=8080)}")
OWNER_ID = int(os.environ.get("WES_DISCORD_OWNER_ID", "0"))
CONV_CHANNEL = "discord"

# Alerting: Prometheus (on the Pi) evaluates the rules in
# observability/prometheus/wes-alerts.yml; this bot only polls the firing set
# and DMs the owner on changes — Prometheus owns thresholds and durations.
PROM_URL = os.environ.get(
    "WES_PROM_URL", wes_hosts.url("pi", "prometheus", default="http://10.0.0.79:9090"))
ALERT_POLL_S = float(os.environ.get("WES_ALERT_POLL_S", "60"))
# DM once if Prometheus itself is unreachable this many polls in a row.
PROM_FAIL_POLLS = 5

# Discord rejects messages over 2000 chars; leave headroom for safety.
DISCORD_MSG_LIMIT = 2000


def authorized(author_id):
    """Only the configured owner may talk to the bot. An unset owner ID
    authorizes nobody — fail closed, never open."""
    return OWNER_ID != 0 and author_id == OWNER_ID


def should_handle(author_id, is_self, is_dm, mentions_bot):
    """Handle DMs from the owner, and guild messages only when mentioned."""
    if is_self or not authorized(author_id):
        return False
    return is_dm or mentions_bot


def chunk_reply(text, limit=DISCORD_MSG_LIMIT):
    """Split a reply into <= limit-char pieces, preferring whitespace breaks."""
    text = text.strip()
    chunks = []
    while len(text) > limit:
        cut = text.rfind(" ", 1, limit + 1)
        if cut <= 0:
            cut = limit
        chunks.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    if text:
        chunks.append(text)
    return chunks


def _post_json(path, body, timeout=120):
    req = urllib.request.Request(
        SERVER_URL + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def ask_server(text):
    """One text turn through the server (blocking; run off the event loop).
    The generous timeout covers a Claude escalation with tool rounds."""
    return _post_json("/respond_text", {"text": text, "channel": CONV_CHANNEL})["reply"]


def reset_server():
    """Clear the Discord conversation channel only — never the voice one."""
    return _post_json("/reset_conversation", {"channel": CONV_CHANNEL})["cleared_turns"]


# What each alert rule (observability/prometheus/wes-alerts.yml) actually means,
# in plain terms — handed to Jarvis so his explanation is grounded, not guessed.
# Keep in sync when rules are added/renamed.
ALERT_CONTEXT = {
    "TargetDown": (
        "Prometheus (on the Pi) has been unable to scrape this metrics target "
        "for 5 minutes. It usually means the exporter process died, the host "
        "is off/asleep, or a firewall/network path broke. For a PC target "
        "(windows_exporter :9182 or nvidia_gpu_exporter :9835) the likely cause "
        "is the 'WES Exporters' scheduled task stopping; for the Pi's own "
        "node_exporter it's more serious. While a target is down, the metrics "
        "and dashboards it feeds are blank."),
    "GPUHot": (
        "The PC's RTX 5060 Ti GPU has been above 85 degrees Celsius for 5 "
        "minutes. The GPU runs the local Gemma models; sustained heat can throttle "
        "inference and shortens hardware life. Worth checking case airflow or "
        "whether something is pinning the GPU."),
    "PiHot": (
        "The Raspberry Pi 5's CPU has been above 80 degrees Celsius for 5 "
        "minutes. The Pi runs the voice client, Prometheus and Grafana; at ~85C "
        "it throttles. Check the Pi's fan/heatsink and ambient temperature."),
    "PiDiskLow": (
        "The Pi's SD card root filesystem has been under 10 percent free for 30 "
        "minutes. If it fills up, logging, Prometheus, and the voice client can "
        "fail. Prometheus data and journald logs are the usual culprits."),
    "PCDiskLow": (
        "The PC's C: drive has been under 10 percent free for 30 minutes. The "
        "WES server, models, and logs live there; a full disk can crash the "
        "server."),
}


def parse_alerts(payload):
    """{alert key: info dict} from Prometheus GET /api/v1/alerts. Keyed by
    (alertname, instance) so the same rule on two targets is two alerts, and a
    still-firing alert keeps a stable key across polls (no repeat DMs). Each
    info carries the rule's own templated `summary` annotation and value."""
    alerts = {}
    for a in payload.get("data", {}).get("alerts", []):
        if a.get("state") != "firing":
            continue
        lbl = a.get("labels", {})
        name = lbl.get("alertname", "?")
        instance = lbl.get("instance", "")
        alerts[f"{name}|{instance}"] = {
            "alertname": name,
            "job": lbl.get("job", instance or "?"),
            "instance": instance,
            "summary": (a.get("annotations", {}).get("summary") or name).strip(),
            "value": a.get("value", ""),
        }
    return alerts


def raw_summary(info, resolved=False):
    """Plain fallback DM used when the server can't be reached to phrase the
    alert — never drop an alert just because the explainer is down."""
    tag = "✅ Resolved" if resolved else "🚨 WES alert"
    return f"{tag}: {info['summary']} [{info['job']}]"


def describe_event(info, resolved=False):
    """The internal event string handed to the server's /announce so Jarvis can
    explain it: the alert, the affected host, the rule's summary, and what the
    rule means (ALERT_CONTEXT)."""
    ctx = ALERT_CONTEXT.get(info["alertname"], "")
    if resolved:
        return (f"A monitoring alert just CLEARED. Alert '{info['alertname']}' "
                f"on {info['job']} ({info['instance']}) is no longer firing. "
                f"It had been: {info['summary']} Let the user know it has "
                f"resolved. Background on the alert: {ctx}")
    return (f"A monitoring alert just STARTED FIRING. Alert '{info['alertname']}' "
            f"on {info['job']} ({info['instance']}). Details: {info['summary']} "
            f"What this alert means: {ctx}")


def explain_event(info, resolved=False):
    """Ask the server to have Jarvis phrase the event in natural English (and
    record it into the discord conversation memory so a follow-up reply has
    context). Blocking; run off the event loop."""
    return _post_json(
        "/announce",
        {"event": describe_event(info, resolved), "channel": CONV_CHANNEL},
    )["reply"]


def fetch_firing_alerts():
    """Poll Prometheus for currently-firing alerts (blocking; run off-loop)."""
    with urllib.request.urlopen(
            f"{PROM_URL}/api/v1/alerts", timeout=10) as r:
        return parse_alerts(json.loads(r.read()))


async def alert_watch(client):
    """DM the owner when an alert starts or stops firing — phrased by Jarvis in
    natural English (falling back to the raw summary if the server is down, so
    an alert is never lost). Also complains once if Prometheus itself has been
    unreachable for PROM_FAIL_POLLS polls."""
    await client.wait_until_ready()
    prev, fails, prom_down = {}, 0, False
    print(f"[alerts] watching {PROM_URL} every {ALERT_POLL_S:g}s", flush=True)

    async def dm(text):
        user = await client.fetch_user(OWNER_ID)
        for chunk in chunk_reply(text):
            await user.send(chunk)

    async def announce_change(info, resolved):
        # Prefer Jarvis's natural phrasing; fall back to the raw summary if the
        # server (the explainer) is itself unreachable — which may be WHY the
        # alert fired, so this path must stay dead simple.
        try:
            text = await asyncio.to_thread(explain_event, info, resolved)
        except Exception as e:  # noqa: BLE001
            print(f"[alerts] explain failed, sending raw: {e!a}", flush=True)
            text = raw_summary(info, resolved)
        print(f"[alerts] dm ({'resolved' if resolved else 'firing'}): "
              f"{text!a}", flush=True)
        await dm(text)

    while not client.is_closed():
        try:
            cur = await asyncio.to_thread(fetch_firing_alerts)
            fails = 0
            if prom_down:
                prom_down = False
                await dm("✅ Monitoring back: Prometheus is reachable again.")
            for key in sorted(k for k in cur if k not in prev):
                await announce_change(cur[key], resolved=False)
            for key in sorted(k for k in prev if k not in cur):
                await announce_change(prev[key], resolved=True)
            prev = cur
        except Exception as e:  # noqa: BLE001 — the watcher must never die
            fails += 1
            print(f"[alerts] poll failed ({fails}): {e!a}", flush=True)
            if fails == PROM_FAIL_POLLS and not prom_down:
                prom_down = True
                try:
                    await dm("🚨 WES alert: can't reach Prometheus on the Pi "
                             f"(down {PROM_FAIL_POLLS} polls) — no monitoring "
                             "until it's back.")
                except Exception as e2:  # noqa: BLE001
                    print(f"[alerts] DM failed: {e2!a}", flush=True)
        await asyncio.sleep(ALERT_POLL_S)


# Fantasy write watcher (#029 P3/P4): DM the owner when a real Yahoo lineup
# write happens — the "content exists, doesn't get pushed" gap named in
# ticket #029. Polls the SAME append-only ledger `wes_execute` writes,
# entirely separately from that module's own process (the scheduled
# "WES Fantasy GM" task) — this bot just watches the file for new entries,
# the same relationship alert_watch has with Prometheus.
FANTASY_POLL_S = float(os.environ.get("WES_FANTASY_POLL_S", "60"))

FANTASY_CONTEXT = (
    "Ticket #029: WES can autonomously manage a fantasy football roster for "
    "teams configured with autonomy 'auto'. This event is that after-action "
    "report — a real write already happened (or was attempted) on the real "
    "Yahoo account; nothing here is asking permission, it's reporting what "
    "was already done."
)


def raw_fantasy_summary(entry):
    """Plain fallback DM used when the server can't be reached to phrase the
    event — never drop a real-write notification just because the explainer
    is down (the same reasoning as raw_summary for alerts)."""
    moves = entry.get("moves") or []
    move_text = "; ".join(f"{m.get('name','?')}: {m.get('from_slot') or '(none)'} "
                          f"-> {m.get('to_slot','?')}" for m in moves) or "(no moves)"
    if entry.get("executed") == "unknown":
        return (f"⚠️ WES fantasy: tried to update {entry.get('name','a team')}'s "
                f"lineup but hit an error partway through — the real Yahoo "
                f"roster may not match what you expect. Check it directly. "
                f"Intended: {move_text}")
    return f"🏈 WES fantasy: updated {entry.get('name','a team')}'s lineup — {move_text}"


def describe_fantasy_event(entry):
    """The internal event string handed to /announce so Jarvis can explain a
    real (or partially-failed) Yahoo write in natural English, grounded in the
    actual moves and the WHY reasoning already computed by
    wes_execute.summarize_moves (never re-derived or guessed here)."""
    team = entry.get("name", "a team")
    moves = entry.get("moves") or []
    move_text = "; ".join(f"{m.get('name','?')}: {m.get('from_slot') or '(none)'} "
                          f"-> {m.get('to_slot','?')}" for m in moves) or "no moves"
    why_text = " ".join(entry.get("why") or []) or "no reasoning recorded"
    if entry.get("executed") == "unknown":
        return (f"WES just ATTEMPTED to update the real Yahoo fantasy lineup "
                f"for '{team}' (autonomous, ticket #029) but hit an error "
                f"partway through. Reason: {entry.get('reason', '?')} Intended "
                f"moves: {move_text}. Tell the owner plainly that the real "
                f"roster may be in an inconsistent state and they should check "
                f"it on Yahoo directly — do not imply it definitely succeeded "
                f"or definitely failed. Background: {FANTASY_CONTEXT}")
    return (f"WES just CHANGED the real Yahoo fantasy lineup for '{team}' "
            f"(autonomous, ticket #029). Moves made: {move_text}. Reasoning: "
            f"{why_text} Tell the owner what changed and why, briefly — this "
            f"already happened, you're reporting it, not proposing it. "
            f"Background: {FANTASY_CONTEXT}")


def explain_fantasy_event(entry):
    """Ask the server to have Jarvis phrase the write in natural English (and
    record it into the discord conversation memory so a follow-up question —
    'why did you bench him' — has context). Blocking; run off the event loop."""
    return _post_json(
        "/announce",
        {"event": describe_fantasy_event(entry), "channel": CONV_CHANNEL},
    )["reply"]


def fetch_new_fantasy_events(since_ts):
    """Ledger entries representing a real write ATTEMPT (`executed` is `True`
    or `"unknown"` — a clean success or a partial/uncertain one; `False` is a
    routine no-op or a blocked proposal and is never notified) with `ts` after
    `since_ts`, oldest first. Tolerant of a missing/unreadable ledger or a
    corrupt line — returns what it can rather than raising, since "no fantasy
    activity yet" is the common case, not an error."""
    path = wes_execute.LEDGER_FILE
    if not os.path.exists(path):
        return []
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("executed") in (True, "unknown") \
                    and entry.get("ts", 0) > since_ts:
                out.append(entry)
    out.sort(key=lambda e: e.get("ts", 0))
    return out


async def fantasy_watch(client):
    """DM the owner when a real (or partially-failed) Yahoo fantasy write
    happens — phrased by Jarvis (falling back to a raw summary if the server
    is down). Mirrors alert_watch's shape exactly; polls a file instead of
    Prometheus. State (`seen_ts`) is in-memory only, seeded to "now" at
    startup — a bot restart does not replay write history from before it
    started, matching alert_watch's no-persistent-state philosophy."""
    await client.wait_until_ready()
    seen_ts = time.time()
    print(f"[fantasy] watching {wes_execute.LEDGER_FILE} every "
          f"{FANTASY_POLL_S:g}s", flush=True)

    async def dm(text):
        user = await client.fetch_user(OWNER_ID)
        for chunk in chunk_reply(text):
            await user.send(chunk)

    async def announce_fantasy(entry):
        try:
            text = await asyncio.to_thread(explain_fantasy_event, entry)
        except Exception as e:  # noqa: BLE001
            print(f"[fantasy] explain failed, sending raw: {e!a}", flush=True)
            text = raw_fantasy_summary(entry)
        print(f"[fantasy] dm: {text!a}", flush=True)
        await dm(text)

    while not client.is_closed():
        try:
            events = await asyncio.to_thread(fetch_new_fantasy_events, seen_ts)
            for entry in events:
                await announce_fantasy(entry)
                seen_ts = max(seen_ts, entry.get("ts", seen_ts))
        except Exception as e:  # noqa: BLE001 — the watcher must never die
            print(f"[fantasy] poll failed: {e!a}", flush=True)
        await asyncio.sleep(FANTASY_POLL_S)


def make_client(discord):
    """Build the discord.Client with handlers attached (factory keeps the
    module importable — and unit-testable — without the discord package)."""
    # Default intents only — the privileged message-content intent is NOT
    # requested (no dev-portal toggle needed): Discord always delivers content
    # for DMs and for messages that @mention the bot, our only two paths.
    client = discord.Client(intents=discord.Intents.default())

    @client.event
    async def on_ready():
        print(f"[discord] logged in as {client.user} (owner={OWNER_ID})", flush=True)
        # on_ready can re-fire on reconnect — start each watcher exactly once.
        if not getattr(client, "_alert_task", None):
            client._alert_task = asyncio.create_task(alert_watch(client))
        if not getattr(client, "_fantasy_task", None):
            client._fantasy_task = asyncio.create_task(fantasy_watch(client))

    @client.event
    async def on_message(message):
        is_dm = message.guild is None
        mentioned = client.user is not None and client.user.mentioned_in(message)
        if not should_handle(
            message.author.id, message.author == client.user, is_dm, mentioned
        ):
            return

        # Strip the leading @mention in guild channels; DMs arrive clean.
        text = message.content
        if client.user is not None:
            text = text.replace(f"<@{client.user.id}>", "").strip()
        if not text:
            return

        if text.lower() in ("!reset", "!new"):
            cleared = await asyncio.to_thread(reset_server)
            await message.channel.send(
                f"Started a new conversation ({cleared} turns cleared).")
            return

        print(f"[discord] {message.author}: {text!r}", flush=True)
        try:
            async with message.channel.typing():
                reply = await asyncio.to_thread(ask_server, text)
        except Exception as e:  # noqa: BLE001
            print(f"[discord] server error: {e!r}", flush=True)
            await message.channel.send(
                "Sorry, I couldn't reach the house right now.")
            return
        for chunk in chunk_reply(reply or "(no reply)"):
            await message.channel.send(chunk)

    return client


def main():
    # Under the scheduled task, stdout is cp1252 — any print of user content
    # or emoji would raise UnicodeEncodeError mid-handler. Log lossy, not fatal.
    import sys
    for s in (sys.stdout, sys.stderr):
        if hasattr(s, "reconfigure"):
            s.reconfigure(encoding="utf-8", errors="replace")
    token = os.environ.get("WES_DISCORD_TOKEN")
    if not token:
        raise SystemExit("WES_DISCORD_TOKEN is not set (see module docstring)")
    if OWNER_ID == 0:
        raise SystemExit("WES_DISCORD_OWNER_ID is not set — the bot would "
                         "answer nobody; set it to your Discord user ID")
    import discord

    make_client(discord).run(token)


if __name__ == "__main__":
    main()
