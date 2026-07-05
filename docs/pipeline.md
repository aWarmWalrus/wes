# Turn lifecycle & pipeline

How one wake-word-to-reply turn flows through `pi/wes_client.py` + `pc/wes_server.py`.

```
Pi: wake word ("hey_jarvis") → VAD-endpointed capture → POST /respond_stream (WAV, stream=True)
PC: faster-whisper STT → LLM (STREAMING, + tools) → per-sentence piper TTS
    → stream raw s16le PCM (22050 Hz mono) back as it's generated
Pi: pipe the PCM into a single `paplay --raw` on the JBL — the first sentence plays
    before the full reply exists
```

## LLM backend (`WES_LLM`)

`WES_LLM=local` (the launcher default since the 5060 Ti 16GB) runs **gemma4:e4b via
Ollama** — streaming + the same tool loop (`_stream_local`), `think: false` so no
thinking tokens are spoken, `keep_alive: -1` so it stays VRAM-resident. Scene
description uses a separate resident VLM, **gemma4:12b** (`WES_VLM_MODEL`) — chat
keeps the faster e4b for time-to-first-audio, while the 12b's extra latency hides
behind the wake-word vision prefetch; both fit the 16GB card (11.4GB). Local errors
before any output fall back to Claude automatically; `WES_LLM=claude` switches back
entirely (`WES_LLM_LOCAL_MODEL` overrides the local model). Local ≈ halves LLM latency
vs Haiku (~700ms vs ~1300ms) and has no API rate limit (speculation budget unlimited).

**Why e4b as the router, not 12b for everything — measured A/B (2026-07-04)**, same
5-run-median probe against the live server: with 12b as the router, plain chat ttfa
went 990→1425ms (+44%) and a tool turn (two model passes) went 1741→2833ms (+63%) —
an extra ~0.4-1.1s of silence on *every* turn. Reply quality on the eval golden set
was unchanged (judge correct-avg 1.78/2, inside e4b's 1.56-2.00 range across runs),
and 12b escalated the hard-reasoning case to Claude just like e4b does. The bigger
router buys audible latency and no measurable quality on the easy tier it would
actually serve; 12b stays vision-only, where the prefetch hides its cost.

**Smart routing** (`WES_ESCALATE=1`, default): the local model is effectively WES's
**router** — every turn lands on it first, it answers the easy tier itself (zero
routing overhead: the route decision and the reply share one forward pass), and
delegates when needed: rich vision to the resident **gemma4:12b** VLM (via the
`describe_scene` tool, usually a prefetch-cache hit), deep reasoning to Claude.
The local toolset additionally carries
an `escalate_to_claude` function (never shown to the deep tier itself), so gemma
decides per-turn when a query is beyond it — deep reasoning, hard math/code,
specialized knowledge — and the server streams the deep tier's reply instead
(`[route] escalating…` in the log). The moment an escalation fires the **server
itself speaks an acknowledgment** (`WES_ESCALATE_ACK`, default "Good question —
let me think about that.", empty disables) so the deep tier's spin-up isn't dead
air — first audio ~1.3s instead of ~4s of silence. It's server-injected, not
model-spoken, because if gemma has already started speaking, the handoff is
suppressed (a tool result tells it to finish itself) so the user never hears two
answers.

**The deep tier is configurable** (`WES_ESCALATE_MODEL`, 2026-07-05): set it to
an Ollama model — the launcher sets **`gemma4:12b` with thinking enabled** —
and escalations are answered fully locally: same tool loop, the shared tools
(minus the escalate function — no recursion), a 2048-token budget to cover the
thinking, and thinking deltas arriving in `message.thinking` which the server
never reads, so they're never spoken. Unset, escalations go to Claude Haiku
(needs the API key), which also remains the automatic fallback on local
*errors* either way. The tool keeps its prompt-tuned `escalate_to_claude`
name — its semantics ("hand off to the much smarter model") don't change with
the target. Note this is different from 12b-as-router (rejected above): the
12b only pays its latency on the hard tier, where the ack masks it. Verified:
"what time is it" stays local, the multi-step train word problem logs
`escalating to gemma4:12b` and comes back correct (~11.5s of hidden thinking).

## STT contextual biasing (added 2026-07-04)

`transcribe()` passes whisper an `initial_prompt` = **domain lexicon**
(`WES_STT_LEXICON`: Jarvis, Raspberry Pi, Hailo, Hue, ecobee, the speaker
names, and the household — Charlie, Cindy, Kaia, Ellis) **+ the tail of the
conversation memory**. The decoder treats it as the transcript-so-far, so
ambiguous audio resolves toward in-context words — the local equivalent of
Google Assistant boosting your contacts. Applies to every STT call (turns and
speculation). Measured A/B on the same audio: "Kaya" → **"Kaia"** fixed;
"Hailo" still transcribes as "Halo" (true homophone — the system prompt tells
the router to interpret charitably instead). Cost ≈ +25ms STT. Keep the
lexicon short: an overlong prompt makes whisper hallucinate prompt words into
marginal audio (the `robust-silence` eval case is the tripwire). Empty
`WES_STT_LEXICON` disables. The `lexicon-*` golden cases gate it.

## Conversation memory (added 2026-07-04)

The server keeps a **sliding-window conversation context** (the LiveKit
ChatContext pattern), keyed by channel: `"voice"` is the house conversation —
one house, one mic — and remote text frontends get their own channel (the
Discord bot uses `"discord"`), so a chat from away never clobbers the in-house
context. Non-voice channels also get `TEXT_CHANNEL_NOTE` appended to the system
prompt (`system_prompt(channel)`), overriding the voice framing — without it the
model tells Discord users it hears them "by voice command". Each channel replays
its last `WES_CONV_TURNS` exchanges (default 6) to whichever backend answers the
next turn. Key behaviors:

- **Shared across the handoff**: escalated turns give Claude the same history
  gemma saw (and gemma later sees what Claude said) — without this, "explain that
  differently" breaks the moment a turn escalates.
- **Idle expiry**: `WES_CONV_TTL` seconds without a turn (default 300) clears the
  context, so this morning's chat doesn't color tonight's question.
- **Silence isn't memory**: empty transcripts and empty replies are never
  recorded; a partial reply on a client abort is (it *is* what the user heard —
  barge-in will tag these `[interrupted]` later, per LiveKit practice).
- **`POST /reset_conversation`** clears it explicitly — one channel via JSON
  `{"channel": ...}`, or every channel on an empty body. The eval harness calls
  it (empty body) before every case so golden cases stay order-independent, and
  it's the hook for a future "new conversation" voice command; the Discord bot's
  `!reset` clears only its own channel.

Measured cost: none — perf_check ttfa after the change (1277ms) is within noise of
the pre-memory baseline (~1800ms median, well inside limits), since a few short
exchanges add only ~100-200 prompt tokens. Verified e2e: a fact stated in turn one
("my favorite color is purple") is recalled in turn two through the full
STT→router→TTS→re-transcription loop, and the `memory-recall` golden case gates it
from now on. Multi-turn eval cases use `turns:` in `golden.yaml`.

Not yet built (see roadmap): persistence across server restarts, and long-horizon
memory (summarizing old turns instead of dropping them — LiveKit's
background-summary pattern) if the window ever feels short.

## Streaming reply (`/respond_stream`, the default path)

The PC streams the LLM's tokens, splits them into sentences (`next_sentence`,
terminator + whitespace), scrubs each for speech (`tts_clean` — strips markdown,
list markers, links, ✓/→-style symbols; the system prompt also asks for plain
spoken English, but models slip), synthesizes each with an **in-process** piper voice
(`PiperVoice.load` once — no per-sentence reload), and writes raw PCM to a chunked
HTTP response. Time-to-first-audio ≈ `STT + Claude-first-sentence + one-sentence-TTS`
(~1.2s warm) and stays flat regardless of reply length. Transcript/timing come back in
`X-Transcript` / `X-Stt-Ms` / `X-Spec` headers. The old `/respond` (blocking, returns
a full WAV) is kept as a fallback; the perf/e2e tests exercise `/respond_stream`,
the production path (one e2e test covers the `/respond` fallback).

## End-of-speech: Silero VAD (not energy)

`record_sentence` uses openwakeword's bundled `silero_vad.onnx`
(`VAD.predict(chunk, frame_size=640)` — **640 divides the 1280 chunk; 480 does NOT**,
which silently corrupted scores when we first tried it) to get a speech probability per
chunk. It **waits for speech to start** (up to `SPEECH_START_TIMEOUT_S=5`, so the pause
after the wake word doesn't end the turn), then stops after `SILENCE_SECONDS=0.8` of
prob < `WES_VAD_THRESHOLD` (0.5). Robust to background noise where the old RMS energy
threshold failed (noise energy ≈ speech energy). `[rec]` logs `speech_prob`/`max_prob`;
`no-speech-timeout` if speech never starts.

## Single-stream playback (`play_turn`)

The streamed reply plays through **one** `paplay --raw` (22050 mono s16le), with
silence written during the STT gap to keep the sink fed. A **stall watchdog** kills
the player only after 30s with *no new reply data* — the deadline resets on every
received chunk, so long replies streaming fine are never cut off; error labeled
`server stalled >30s (watchdog)` vs a genuine `sink drop`. At startup the client
blocks in `wait_for_server()` (polls PC `/health` every 3s) before entering the
ready state, so it never listens while the server is down or still warm-loading.
No filler/"thinking word" — it was removed as clunky.
**Why one stream:** the Bluetooth A2DP transport wedges if acquired/released
repeatedly — see the persistent-silence fix in `docs/audio.md`.

## Speculative prefetch (`/speculate`)

While recording, the Pi POSTs the audio-so-far every 2s (`SPECULATE_INTERVAL_S`). The
PC transcribes each partial and fires a **background Claude call**, caching the reply
keyed by normalized transcript (`speculate_async`, 60s TTL). On finalize,
`/respond_stream` calls `lookup_speculation` — a match (exact, or a cached partial
covering ≥85% of the final) **skips Claude entirely** and streams TTS of the cached
reply (`X-Spec: HIT`). Worst case is a wasted Claude call; we never play speculatively.
**Speculation is disabled when tools are on** (`spec=tools`) — a tool-less speculative
reply could be wrong for a state query.

## Pi-introspection tools (streaming tool loop)

Claude can query live Pi state mid-answer. `stream_reply` runs a streaming tool loop
(`WES_TOOLS=1`, `MAX_TOOL_ROUNDS=4`): each Claude turn streams to TTS; on
`stop_reason == tool_use` it runs the tool (`run_tool` → HTTP to the Pi's `:8090`),
feeds the result back, and continues. Tools are **read-only**.

- `get_system_status` → Pi `/state` (temp, throttle, load, mem, disk, uptime, BT).
- `get_datetime` → local time (no network).
- `read_pi_log` → Pi `/logs?service=&n=` (whitelisted: bluetooth, wireplumber,
  pipewire, kernel).
- `look` / `describe_scene` — vision tools, see `docs/vision.md`.

Adding a tool = a schema entry in `TOOLS` + a branch in `run_tool` (+ a unit test).
Tool round-trips add ~2s (extra Claude call + Pi fetch).

## Status LED (Pi client)

The C920's front LED is a status light (the camera never streams for this):
**On** on wake word / **Off** when the reply finishes / **Blink** while the JBL is
disconnected. Controlled via `uvcdynctrl -d /dev/video0 -s "LED1 Mode"
<0=Off|1=On|2=Blink|3=Auto>`. `state["bt"]` gates the turn's LED calls so they don't
stomp the disconnect blink.

## Telemetry

Every turn is timed → appended to `~/wes/logs/timing.csv` and printed as a `[timing]`
line. The PC returns per-stage timings in headers (`X-Stt-Ms`, `X-Llm-Ms`, `X-Tts-Ms`).
Key CSV columns (ms): `record_ms`, `stt_ms`/`llm_ms`/`tts_ms`, `ttfa_ms`,
`gap_to_reply_ms` (end-of-speech → first reply audio), `reply_play_ms`, `spec` (HIT/
miss/tools). Quick look: `column -s, -t ~/wes/logs/timing.csv | less -S`. Track latency
over time with `tests/perf_check.py`.
