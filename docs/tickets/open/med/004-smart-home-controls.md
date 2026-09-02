---
id: 004
title: Smart home controls — Hue-direct first, Home Assistant later
status: open
priority: med
created: 2026-07-04
closed:
tags: [smart-home, tools, safety]
related: [wes-hue-pairing-pending (memory), "#005"]
---

## Goal
Give the router tools that *act*, not just observe. Natural fit for the tool
loop: e4b routes tool calls well, and lights/switches/thermostat commands are
easy-tier (no escalation needed).

## Context — LAN discovery scan (2026-07-04, mDNS + SSDP from the PC)
Found: **Philips Hue Bridge** "Home" (10.0.0.143, BSB002, API 1.77 — local REST,
lights), **ecobee thermostat** "Living Room" (10.0.0.155, ECB601 — local control
only via HomeKit → needs Home Assistant), and four Cast devices (Chromecast Ultra
"Stevie" .106, Nest Hub Max "Kitchen Display" .130, plus the off-limits speakers
Matcha .23 / Good gray .238). No existing HA, no Matter/Sonos/Kasa/Shelly.

## Approach (phased)
1. **Hue-direct first, no HA**: one-time link-button pairing mints an API key
   (→ PC user env, like the Anthropic key), then two tools (`get_lights`,
   `set_light`) over plain local HTTP. *Pending: pressing the physical bridge
   button to pair.*
2. **Home Assistant later** for the ecobee + a unified layer (the only found
   device that requires it). Pi 5 can host it — watch RAM alongside the WES
   client; PC is the fallback host.
3. Cast media control (pause/volume — NOT playback initiation to the off-limits
   speakers) via the existing catt/pychromecast stack.

## Safety rails
Read-only states are free; *actions* start with an allowlist of harmless domains
(lights, switches, media players — **not** locks, garage doors, or anything
security-relevant) and confirm-before-acting outside it. Same spirit as the
house audio rule. The Discord bot must stay behind a per-action confirm before
any of this is exposed remotely (#016 done-ticket notes this).

## Acceptance
- [ ] Hue paired, `get_lights`/`set_light` working locally
- [ ] golden cases per action (exact tool + entity assertion; wants phase-3
      X-Tools header) against a MOCK endpoint so the eval never flips real devices
- [ ] action allowlist + confirm rail enforced server-side

## Notes
Add hosts to `hosts.yaml` if any of these become code-referenced.

## Update 2026-09-02 — two premises in the approach are gone

The Raspberry Pi was repurposed (`archive/pi/README.md`), which invalidates two
things written above:

- **"Pi 5 can host it"** for Home Assistant. There is no Pi. Home Assistant
  would have to run on the PC (Docker, alongside the monitoring stack in
  `observability/`) or on new hardware.
- **"via the existing catt/pychromecast stack"**. That stack no longer exists:
  `PyChromecast` came out of `pc/requirements.txt` and the cast code out of
  `wes_server.py` with the rest of the audio path. Casting a *notification* to a
  Nest device is still perfectly possible, but it is now new work rather than
  reuse.

The **house audio rule** it cites also needs restating rather than deleting:
WES has no speaker of its own any more, so "never play audio without
confirmation" now applies to anything it might cast to someone else's device —
which is a stronger version of the same rule, not a weaker one.
