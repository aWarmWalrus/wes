---
id: 001
title: Discord router answers vision questions without calling describe_scene
status: open
priority: high
created: 2026-07-05
closed:
tags: [router, vision, discord, prompt]
related: [docs/observability.md, "#003"]
---

## Problem
On the Discord (text) channel, "use the camera to describe what you see" gets an
invented answer with **no tool call**. Diagnosed 2026-07-05 via the turn log.

Evidence (`/turns`): the query was answered in 1.1s with `tools: []` — a real
`describe_scene` capture takes 10-18s, so the living-room description was
fabricated. The whole 17:08 Discord conversation is the same: "I used my
description tool again" while never calling anything, even contradicting live
face-rec (said "no people" while the wake-word prefetch had recognized charlie).

`describe_scene` itself works from any channel (fresh Pi capture on cache miss)
— the router just doesn't invoke it on the text channel.

## Approach
Likely contributor: `TEXT_CHANNEL_NOTE` frames the user as "away from home" and
says nothing about tools still being live; multi-turn context then normalizes
answering from imagination.
- Add an explicit line to `TEXT_CHANNEL_NOTE`: you still have live camera/tool
  access to the house; any question about what you can SEE right now requires
  calling `describe_scene` or `look` — you have no visual memory.
- Add a discord-channel vision golden case that asserts the tool actually ran
  (needs an `X-Tools` header or a `/turns` assertion — eval phase 3).
- Consider a server-side guard: a vision-y query that produced zero vision tool
  calls → retry with a nudge.

## Acceptance
- [ ] "what do you see" on the discord channel calls `describe_scene`/`look`
- [ ] golden case fails if a vision query answers with no vision tool call
- [ ] full eval (`--judge local`) green after the prompt change

## Notes
Any prompt/routing change needs the full eval + a new golden case (testing
rigor). The turn log is the tool that surfaced this — use it to verify.

**Broader than vision (2026-07-06):** the same failure hit the new `remember`
tool — "Please remember X" on Discord returned "I've remembered X" with no
`[tool] remember` call, so nothing was saved (voice called it fine). So this is
really "**e4b under-calls tools on the text/Discord channel and narrates the
action instead**", affecting vision AND memory writes. The fix (explicit
"you must call the tool, don't just claim you did" framing — a channel-neutral
nudge was added to SOUL.md 2026-07-06 but isn't sufficient on its own — plus
maybe a server-side "claimed-an-action-without-the-tool → retry" guard) should
cover both. Consider raising priority given it now blocks memory writes too.
