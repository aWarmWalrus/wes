---
id: 016
title: Remote access via Discord bot
status: done
priority: high
created: 2026-07-05
closed: 2026-07-05
tags: [discord, frontend, remote]
related: [docs/setup.md, tests/test_unit_discord.py, "#004", "#005"]
---

## What shipped
Talk to Jarvis by text when away. New *frontend*, not a pipeline change: server
gained `POST /respond_text` (JSON text-in/out) + per-channel memory (#015);
`pc/wes_discord.py` bridges owner DMs (and guild @mentions) to it, chunked to
Discord's 2000-char limit, `!reset` starts a new conversation. Runs as the "WES
Discord" scheduled task; RUNNING as "Jarvis WES#0810".

## Auth / rails
Hard allowlist of the owner's Discord user ID (fails closed when unset); everyone
else ignored silently. Default intents only (no privileged message-content — DMs
and @mentions are exempt). Internet-facing door into the tool loop → stays
read/answer-only until action rails exist. **No smart-home actions over Discord
without a per-action confirm** (gates #004).

## Outcome
Verified e2e (fact recall across turns on the "discord" channel, scoped reset).
Later gained proactive alert DMs (#021).
