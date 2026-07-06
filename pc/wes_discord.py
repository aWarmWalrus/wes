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
"""
import asyncio
import json
import os
import urllib.parse
import urllib.request

SERVER_URL = os.environ.get("WES_SERVER_URL", "http://127.0.0.1:8080")
OWNER_ID = int(os.environ.get("WES_DISCORD_OWNER_ID", "0"))
CONV_CHANNEL = "discord"

# Alerting: Prometheus (on the Pi) evaluates the rules in
# observability/prometheus/wes-alerts.yml; this bot only polls the firing set
# and DMs the owner on changes — Prometheus owns thresholds and durations.
PROM_URL = os.environ.get("WES_PROM_URL", "http://10.0.0.79:9090")
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


def parse_alerts(payload):
    """{alert key: human summary} from a Prometheus /api/v1/query response for
    ALERTS{alertstate="firing"}. Keyed by (alertname, instance) so the same
    rule firing on two targets DMs twice, and a still-firing alert is stable
    across polls (no repeat DMs)."""
    alerts = {}
    for r in payload.get("data", {}).get("result", []):
        m = r.get("metric", {})
        key = f'{m.get("alertname", "?")}|{m.get("instance", "")}'
        alerts[key] = (f'{m.get("alertname", "?")}'
                       f' [{m.get("job", m.get("instance", "?"))}]')
    return alerts


def alert_messages(fired, resolved):
    """DM texts for state changes. fired/resolved: {key: summary} dicts."""
    msgs = [f"🚨 WES alert: {s}" for s in sorted(fired.values())]
    msgs += [f"✅ Resolved: {s}" for s in sorted(resolved.values())]
    return msgs


def fetch_firing_alerts():
    """Poll Prometheus for currently-firing alerts (blocking; run off-loop)."""
    q = urllib.parse.quote('ALERTS{alertstate="firing"}')
    with urllib.request.urlopen(
            f"{PROM_URL}/api/v1/query?query={q}", timeout=10) as r:
        return parse_alerts(json.loads(r.read()))


async def alert_watch(client):
    """DM the owner when an alert starts or stops firing. Also complains once
    if Prometheus itself has been unreachable for PROM_FAIL_POLLS polls."""
    await client.wait_until_ready()
    prev, fails, prom_down = {}, 0, False
    print(f"[alerts] watching {PROM_URL} every {ALERT_POLL_S:g}s", flush=True)

    async def dm(text):
        user = await client.fetch_user(OWNER_ID)
        await user.send(text)

    while not client.is_closed():
        try:
            cur = await asyncio.to_thread(fetch_firing_alerts)
            fails = 0
            if prom_down:
                prom_down = False
                await dm("✅ Resolved: Prometheus is reachable again.")
            fired = {k: v for k, v in cur.items() if k not in prev}
            resolved = {k: v for k, v in prev.items() if k not in cur}
            for msg in alert_messages(fired, resolved):
                # !a: console prints must be ASCII-safe — under the scheduled
                # task stdout is cp1252, and a raw emoji print raises
                # UnicodeEncodeError, which killed this task silently once
                # (2026-07-05: the first real alert died on its own 🚨).
                print(f"[alerts] dm: {msg!a}", flush=True)
                await dm(msg)
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
        # on_ready can re-fire on reconnect — start the watcher exactly once.
        if not getattr(client, "_alert_task", None):
            client._alert_task = asyncio.create_task(alert_watch(client))

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
