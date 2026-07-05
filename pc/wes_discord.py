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
"""
import asyncio
import json
import os
import urllib.request

SERVER_URL = os.environ.get("WES_SERVER_URL", "http://127.0.0.1:8080")
OWNER_ID = int(os.environ.get("WES_DISCORD_OWNER_ID", "0"))
CONV_CHANNEL = "discord"

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
