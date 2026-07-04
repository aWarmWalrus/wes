# Audio output stack

Two output paths: the **JBL over Bluetooth** (default, low latency) and **Google Cast**
(fallback). Output mode is `WES_OUTPUT` on the PC service.

- **`return`** (default) — the PC returns TTS PCM/WAV in the HTTP response; the Pi
  plays it locally on its default sink (the JBL). Much lower latency than casting (no
  discovery/fetch/buffer).
- **`cast`** — the PC casts to a Google Home/Nest (`WES_CAST_DEVICE`). Higher latency;
  fallback. `/health` reports the active mode.

## Bluetooth JBL Flip 5 — the primary speaker

- MAC `28:FA:19:2F:0F:76`, paired+trusted on the Pi, A2DP sink
  `bluez_output.28_FA_19_2F_0F_76.1` (PipeWire), default sink.
- **Pairing gotchas**: the Flip 5 bonds to one source at a time; a stale bond causes
  `AuthenticationFailed`. Factory-reset the JBL (hold Vol+ and Play ~5s), then pair in
  **one** `bluetoothctl` session with a `NoInputNoOutput` agent (`agent
  NoInputNoOutput` → `default-agent` → `pair` → `trust` → `connect`). Separate one-shot
  `bluetoothctl` calls drop the agent and fail SSP.
- `bt_monitor` (Pi client daemon thread) checks the JBL every 5s; on a drop it blinks
  the LED and runs `bluetoothctl connect`; on reconnect it restores the default sink
  and clears the LED.

## A2DP stability — the persistent-silence fix (important)

**Symptom:** BlueZ↔WirePlumber wedges the A2DP transport — `Acquire …
org.bluez.Error.NotAuthorized`, and it can't release it either, so `paplay` hangs
forever writing to a dead sink (a turn stalls after `[audio] turn player started`).

**Cause:** acquiring/releasing the transport repeatedly (which happened per-turn, and
even the single `play_turn` acquisition triggered it).

**Fix:** hold the transport open **permanently**. `start_silence()` runs
`pacat … /dev/zero` to the JBL sink for the client's lifetime (managed by `bt_monitor`,
restarted on reconnect). Per-turn playback then just mixes into the already-active sink
and never re-acquires the transport. Verified: 5 back-to-back plays, 0 NotAuthorized.

- **Recovery** if it ever still wedges: `bluetoothctl disconnect` + `connect` clears it.
- **Downside:** the JBL is always "playing" silence — fine plugged in, drains battery
  otherwise. (Alternative, not done: disable PipeWire suspend-on-idle for the BT sink.)
- No conflicting BT-audio manager runs (bluealsa/pulseaudio inactive); PipeWire's
  `bluez5` module handles it.

## Google Cast (fallback path)

Cast devices play media from a **URL**: generate TTS file → serve over local HTTP →
tell the speaker to fetch+play. Tooling: [`catt`](https://github.com/skorokithakis/catt)
in `~/cast-venv` (`~/cast-venv/bin/catt scan` / `... -d "<device>" cast <url-or-file>`;
casting a local file makes catt serve it over HTTP). ~1–2s startup latency per playback.

Cast devices on the LAN (last scan):

| Name | Type | IP | Use |
|------|------|----|-----|
| Good gray | Nest Mini | 10.0.0.238 | ⛔ off-limits |
| Kitchen Display | Nest Hub Max | 10.0.0.130 | speaker/display (ask first) |
| Matcha | Home Mini | 10.0.0.23 | ⛔ off-limits |
| Stevie | Chromecast Ultra | 10.0.0.106 | video (via TV) |

⚠️ **Never cast/play to any device without explicit per-time confirmation.** Good gray
and Matcha are off-limits for testing under any circumstances.

## `speak.py` (standalone/legacy cast helper)

`text → piper TTS (WAV) → cast to a speaker`. Voice `en_US-amy-medium`
(`~/wes/voices/`), default target Kitchen Display. Run:
`~/cast-venv/bin/python ~/wes/speak.py "text"`. The main pipeline uses PC-side piper +
the JBL, not this; kept for direct Pi-side casting.
