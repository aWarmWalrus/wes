"""Discord frontend for WES — talk to Jarvis by text when away from home.

A thin bridge, not a pipeline: forwards the owner's Discord messages to the
local server's text endpoint (`POST /respond_text`, channel "discord") and
posts the reply back. Since the Raspberry Pi tier retired (2026-09-02) this is
the ONLY interactive frontend WES has — it used to be the away-from-home
alternative to talking to the speaker in the house.

Auth is a hard allowlist of one Discord user ID — everyone else is ignored
silently. This process is an internet-facing door into the tool loop, so it
stays read/answer-only by design (the server exposes no action tools yet;
revisit the rails here before it does).

Run (PC, same venv as the server):
    C:\\Users\\awarm\\wes-pc\\.venv\\Scripts\\python.exe C:\\Users\\awarm\\wes\\pc\\wes_discord.py

Env:
    WES_DISCORD_TOKEN     bot token (Discord developer portal; user env, like
                          the Anthropic key — never the repo)
    WES_DISCORD_OWNER_ID  the one Discord user ID allowed to talk to the bot
    WES_SERVER_URL        default http://127.0.0.1:8080
    WES_PROM_URL          Prometheus for the alert watcher (default
                          http://127.0.0.1:9090); WES_ALERT_POLL_S interval
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

# Everything runs on this PC now, so both of these are loopback. Prometheus was
# on the Pi until 2026-09-02; it moved here in Docker with the rest of the
# monitoring stack (observability/docker-compose.yml).
SERVER_URL = os.environ.get(
    "WES_SERVER_URL",
    f"http://127.0.0.1:{wes_hosts.port('pc', 'server', default=8080)}")
OWNER_ID = int(os.environ.get("WES_DISCORD_OWNER_ID", "0"))
CONV_CHANNEL = "discord"

# Alerting: Prometheus evaluates the rules in
# observability/prometheus/wes-alerts.yml; this bot only polls the firing set
# and DMs the owner on changes — Prometheus owns thresholds and durations.
#
# Loopback, NOT the registry's `pc` address: hosts.yaml gives the PC an mDNS
# name (DESKTOP-R2PFF9T.local) so that off-box scrapers survive its flapping
# DHCP lease, and resolving that from the box itself is a needless dependency on
# mDNS working. The port still comes from the registry.
PROM_URL = os.environ.get(
    "WES_PROM_URL",
    f"http://127.0.0.1:{wes_hosts.port('pc', 'prometheus', default=9090)}")
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
    """Clear the Discord conversation channel only — never another channel's."""
    return _post_json("/reset_conversation", {"channel": CONV_CHANNEL})["cleared_turns"]


# What each alert rule (observability/prometheus/wes-alerts.yml) actually means,
# in plain terms — handed to Jarvis so his explanation is grounded, not guessed.
# Keep in sync when rules are added/renamed.
ALERT_CONTEXT = {
    "TargetDown": (
        "Prometheus has been unable to scrape this metrics target for 5 "
        "minutes. It usually means the exporter process died or a firewall or "
        "network path broke. For windows_exporter (:9182) or "
        "nvidia_gpu_exporter (:9835) the likely cause is the 'WES Exporters' "
        "scheduled task stopping; for the WES server's own metrics it means the "
        "server is down, which matters much more. While a target is down, the "
        "metrics and dashboards it feeds are blank."),
    "GPUHot": (
        "The PC's RTX 5060 Ti GPU has been above 85 degrees Celsius for 5 "
        "minutes. The GPU runs the local Gemma models; sustained heat can throttle "
        "inference and shortens hardware life. Worth checking case airflow or "
        "whether something is pinning the GPU."),
    # PiHot and PiDiskLow lived here until 2026-09-02. The Pi was repurposed;
    # its rules went with it (observability/prometheus/wes-alerts.yml).
    "PCDiskLow": (
        "The PC's C: drive has been under 10 percent free for 30 minutes. The "
        "WES server and its logs live there; a full disk can crash the server."),
    "EDiskLow": (
        "The PC's E: drive has been under 10 percent free for 30 minutes. The "
        "Ollama model store and the conda environment live there, so a full E: "
        "stops the local model from loading at all."),
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
                    await dm("🚨 WES alert: can't reach Prometheus "
                             f"(down {PROM_FAIL_POLLS} polls) — no monitoring "
                             "until it's back. It runs in Docker on this PC; "
                             "`docker compose ps` in observability/ will say "
                             "whether the container is up.")
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
    if _is_recommendation(entry):
        why = "; ".join(entry.get("why") or []) or "see the log"
        return (f"💡 WES fantasy suggestion for {entry.get('name','your team')}: "
                f"{why} (nothing was changed — reply if you want it done)")
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
    if _is_recommendation(entry):
        why_text = " ".join(entry.get("why") or []) or "no reasoning recorded"
        return (f"WES checked the fantasy roster for '{team}' on its scheduled "
                f"run and thinks a change is worth making. NOTHING HAS BEEN "
                f"DONE — this is a suggestion only. Recommendation: {why_text} "
                f"Tell the owner what it suggests and why, briefly, and make "
                f"clear it hasn't happened and needs their go-ahead. Dropping a "
                f"player is permanent, so do not imply it's already done or "
                f"that it will happen on its own. Background: {FANTASY_CONTEXT}")
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
        # use_tools=False: the event text already contains every fact needed
        # (moves + the WHY summary), and ANNOUNCE_FRAMING forbids inventing
        # anything beyond it — so a tool call here would be wrong by
        # definition. Alerts keep their tools; see wes_server.announce.
        {"event": describe_fantasy_event(entry), "channel": CONV_CHANNEL,
         "use_tools": False},
    )["reply"]


def _recommendation_signature(entry):
    """Stable identity of a RECOMMENDATION: which drop/add pairs it proposes.
    Two runs proposing the same swap have the same signature even though their
    timestamps and point estimates differ slightly."""
    return tuple(sorted(
        (str(m.get("drop", "")), str(m.get("add", "")))
        for m in entry.get("moves") or []))


def _is_recommendation(entry):
    """A roster suggestion nobody acted on — as opposed to a real write."""
    return (entry.get("action_type") == "add_drop"
            and entry.get("executed") is False
            and bool(entry.get("moves")))


def fetch_new_fantasy_events(since_ts):
    """Ledger entries NEWER than `since_ts` that are worth telling the owner
    about, oldest first. Two kinds:

    1. **Real write attempts** (`executed` True or `"unknown"`) — something
       actually changed, or may have.
    2. **Roster RECOMMENDATIONS** (#035) — nothing changed, but Jarvis thinks
       something should.

    A routine no-op (`moves: []`) and a guardrail-blocked proposal are never
    notified. Tolerant of a missing/unreadable ledger or a corrupt line.

    This answers "what's NEW by time" only. Whether a recommendation is worth
    REPEATING is `fantasy_watch`'s call — see the dedup there."""
    path = wes_execute.LEDGER_FILE
    if not os.path.exists(path):
        return []
    entries = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    entries.sort(key=lambda e: e.get("ts", 0))

    out = []
    for entry in entries:
        ts = entry.get("ts", 0)
        if ts <= since_ts:
            continue
        if _is_recommendation(entry) or \
                entry.get("executed") in (True, "unknown"):
            out.append(entry)
    return out


async def fantasy_watch(client):
    """DM the owner when a real Yahoo fantasy write happens, or when the
    scheduled run SUGGESTS a roster move (#035) — phrased by Jarvis, falling
    back to a raw summary if the server is down.

    Mirrors alert_watch's shape; polls a file instead of Prometheus. Both bits
    of state are in-memory only, matching alert_watch's no-persistent-state
    philosophy:

    * `seen_ts` starts at "now", so a restart doesn't replay old history.
    * `notified_sig` remembers the last SUGGESTION actually DMed per team, so a
      standing recommendation doesn't nag every morning — the scheduled run
      fires daily and would otherwise re-send "drop Addison for Washington"
      forever. This is the same reason alert_watch notifies on state CHANGE
      rather than on every poll.

    Keeping that dedup in memory rather than deriving it from the ledger is
    deliberate, and was chosen after getting it wrong: reading history from the
    ledger looked more robust, but it meant entries written BEFORE the notifier
    existed silenced a suggestion the owner had never actually been told about
    (observed 2026-07-31 — eight identical dev-run rows swallowed the first
    real DM). In-memory state re-notifies once after a restart, which is
    exactly what alert_watch already does with firing alerts, and is the
    cheaper failure."""
    await client.wait_until_ready()
    seen_ts = time.time()
    notified_sig = {}
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
                seen_ts = max(seen_ts, entry.get("ts", seen_ts))
                if _is_recommendation(entry):
                    team = entry.get("team_key", "")
                    sig = _recommendation_signature(entry)
                    if notified_sig.get(team) == sig:
                        continue          # same standing suggestion — stay quiet
                    notified_sig[team] = sig
                await announce_fantasy(entry)
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


def certifi_default_ssl_context():
    """Make `ssl.create_default_context()` use certifi instead of the Windows
    certificate store. Must run BEFORE `import discord`.

    On this machine's conda Python (3.11.5 + OpenSSL 3.5.7), loading the
    Windows store fails for EVERY certificate:

        ssl.SSLError: [ASN1: NOT_ENOUGH_DATA] not enough data

    The store itself is healthy — all 136 entries parse, and a Python built
    against OpenSSL 3.0 reads them fine. OpenSSL 3.5.7 rejects the DER
    `cadata` path that CPython's `_load_windows_store_certs` feeds it (a
    single known-good DigiCert root from certifi fails the same way), so any
    default-context creation in this env dies. `wes_http` sidesteps it with
    `cafile=certifi.where()` (2026-08-30, same symptom), but aiohttp builds
    its default contexts at IMPORT time in `aiohttp.connector`, so
    `import discord` crashes before a custom connector could ever be passed —
    the default-context factory itself has to be fixed first.

    The wrapper only fills in certifi when the caller gave no CA source of
    its own, and only for server-auth contexts, so explicit configuration
    still wins. No-op (keeps the stock factory) when certifi is missing."""
    import ssl
    try:
        import certifi
    except ImportError:
        return
    orig = ssl.create_default_context

    def patched(purpose=ssl.Purpose.SERVER_AUTH, *,
                cafile=None, capath=None, cadata=None):
        if (cafile is None and capath is None and cadata is None
                and purpose == ssl.Purpose.SERVER_AUTH):
            cafile = certifi.where()
        return orig(purpose, cafile=cafile, capath=capath, cadata=cadata)

    ssl.create_default_context = patched


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
    certifi_default_ssl_context()  # before import discord — see its docstring
    import discord

    make_client(discord).run(token)


if __name__ == "__main__":
    main()
