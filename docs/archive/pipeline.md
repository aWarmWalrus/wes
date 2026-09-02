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

`WES_LLM=local` (the launcher default since the 5060 Ti 16GB) runs **gemma4:12b via
Ollama** — streaming + the tool loop (`_stream_local`), `keep_alive: -1` so it stays
VRAM-resident. **One model serves everything**: the router (chat), the escalation
deep tier (with thinking), and the `describe_scene` VLM (`WES_VLM_MODEL`). Local
errors before any output fall back to Claude automatically; `WES_LLM=claude` switches
back entirely (`WES_LLM_LOCAL_MODEL` overrides the local model). Local ≈ halves LLM
latency vs Haiku and has no API rate limit (speculation budget unlimited).

> **Topology history — was TWO models, now ONE (2026-07-16).** The design was a
> split: **gemma4:e4b** as the fast router + **gemma4:12b** for escalation/vision.
> A measured A/B (2026-07-04) justified it: making 12b the router cost +44% chat
> ttfa / +63% tool-turn latency for no eval-quality gain, so e4b served the easy
> tier and 12b only paid its latency on the hard tier (where the ack masks it).
> Then the `gemma4:e4b` tag vanished from Ollama — every router call 404'd and
> **silently fell back to Claude for a week** (`/health` echoes config, not
> reality). It was collapsed to **12b alone** as the project pivoted toward
> batch/analysis (voice latency de-prioritized). `gemma3:4b` has vision but **no
> `tools`**, so it can't be the router. Guard: **`wes-dev.ps1 models check`** after
> any model change catches exactly this drift.

**Smart routing** (`WES_ESCALATE=1`, default): every turn lands on the local model
first — it answers the easy tier itself (zero routing overhead: the route decision
and the reply share one forward pass) and delegates when needed. The local toolset
carries an `escalate_hard` function (never shown to the deep tier itself), so
the router decides per-turn when a query is beyond a plain pass — deep reasoning,
hard math/code, specialized knowledge — and the server streams the **deep tier's**
reply instead (`[route] escalating…` in the log). Since router and deep tier are now
the *same* 12b, escalation buys **thinking + a bigger token budget**, not a bigger
model. The moment an escalation fires the **server itself speaks an acknowledgment**
(`WES_ESCALATE_ACK`, default "Good question — let me think about that.", empty
disables) so the deep tier's spin-up isn't dead air. It's server-injected, not
model-spoken: if the router has already started speaking, the handoff is suppressed
(a tool result tells it to finish itself) so the user never hears two answers.

**The deep tier is configurable** (`WES_ESCALATE_MODEL`, 2026-07-05): the launcher
sets **`gemma4:12b` with thinking enabled** — escalations answer fully locally (same
tool loop, shared tools minus the escalate function so there's no recursion, a
`WES_DEEP_NUM_PREDICT`-token output budget (default 2048) to cover the thinking +
answer — this is `num_predict`, an OUTPUT cap, NOT the VRAM knob `num_ctx`, so it's
time-bound not memory-bound. Kept modest deliberately: a genuinely hard question
thinks past any budget and emits nothing, so a bigger budget just delays the Claude
fallback (measured 54s@2048 vs 102s@4096) — fail fast to the better+faster tier
(#026 is the adaptive fix). Thinking deltas arrive in `message.thinking`
which the server never reads, so they're never spoken). Unset, escalations go to
Claude Haiku (needs the API key), which also remains the automatic fallback on local
*errors* either way. The tool is named `escalate_hard` for what it *does* (hand a
hard question up), not for a backend — it routes wherever `WES_ESCALATE_MODEL`
points, so the name doesn't claim "Claude" when the target is the local 12b. Its
prompt framing ("a much more capable model") is what gets the small router to hand
off and is unchanged by the target. *(Renamed from `escalate_to_claude` 2026-07-21 —
it was misleading: with the local deep tier configured it never reached Claude.)* **Empty-reply
guard (2026-07-21):** a hard problem can make the deep tier spend its whole budget on
`message.thinking` and emit NO visible content (an empty reply → "(no reply)" on
Discord); `_stream_local` detects `not yielded and deep` and **falls back to Claude**
so the turn gets a real answer instead of dead air (it still costs the wasted thinking
time first — an adaptive budget is #026). Verified:
"what time is it" stays a plain pass; the multi-step train word problem logs
`escalating to gemma4:12b` and comes back correct after hidden thinking.

**Live-info web search → Haiku** (`WES_WEB_SEARCH=1`, default on when the API key is
present; 2026-07-20). Reasoning and *current facts* are split across two handoffs.
Alongside `escalate_hard` (hard reasoning → local 12b+thinking, free), the router
carries a **`search_web`** function for things that need the internet — today's
weather/news/prices/scores, recent events, anything past the model's training. It
routes to **Claude Haiku with Anthropic's server-side web search**
(`web_search_20250305` — the basic variant Haiku 4.5 supports; Claude runs the
searches, the server relays the streamed text). Rationale (owner, 2026-07-20):
reasoning stays free/local, only live lookups pay for Claude. `search_web` is offered
in BOTH the fast router and the deep tier (even the 12b can't reach the web);
`escalate_hard` is fast-router-only (the deep tier already IS the reasoning
escalation, so it must not recurse). The handoff is invisible like escalation:
`WES_WEB_SEARCH_ACK` masks spin-up on voice, and `WES_WEB_SEARCH_NUDGE` tells Haiku to
answer directly without narrating the search ("I'll search…"). Set `WES_WEB_SEARCH=0`
to drop the tool. Verified live: "weather in London", "who won the last F1 race",
"price of bitcoin" all answer from real results; math/reasoning still routes to the
local 12b, not the web.

**Per-channel reasoning tier** (`WES_DEEP_CHANNELS`, default `discord`, 2026-07-07):
latency-tolerant TEXT channels run the **deep tier (12b + thinking) as their router
on every turn**, not just on escalation — `_channel_deep()`. Discord is async, so
trading fast replies for ~15-25s replies buys markedly better reasoning AND far more
reliable tool-calling (bug #001: the router tended to *narrate* tool actions on text
— "I've remembered that" / an invented scene — without actually calling
`remember`/`describe_scene`; thinking mode calls them). Set `WES_DEEP_CHANNELS=""` to
route every channel as a plain pass. (`_channel_deep()` is gated on
`WES_ESCALATE_MODEL` being set — since router and deep tier are the same model now,
this toggles thinking-on-by-default for the channel, not a model swap.)

**VRAM / context** (`WES_NUM_CTX`, default 16384): every Ollama call bounds the
context window. Without it Ollama reserves the model's *native* 256K context, whose
KV cache alone eats ~14GB. Bounded to 16K, the single 12b measures **7.0GB weights,
7.8GB resident, ~6.3GB free** (~50MB KV per 1k ctx) — comfortable on the 16GB card
(the old tight e4b+12b coexistence is gone with the second model). Lower
`WES_NUM_CTX` or `WES_CONV_TURNS_DISCORD` for extra gaming headroom.

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
context. The system prompt (`system_prompt(channel)`) is a **channel-agnostic
persona** (`soul_prompt()`, from `SOUL.md` or the in-code `SYSTEM_PROMPT`
fallback) plus exactly one presentation note — `VOICE_CHANNEL_NOTE` (spoken:
short, no symbols, numbers said aloud) or `TEXT_CHANNEL_NOTE` (typed: digits as
digits, never mention voice) — plus the unified durable **memory block**
(`MEMORY.md` — semantic facts written by the `remember`/`forget` tools, injected
on every channel so a fact learned on Discord is known on voice;
`docs/memory-design.md`) and the live scene. Each channel replays
its recent exchanges to whichever backend answers the next turn. **Depth and
idle TTL are per-channel** (`_conv_policy`): voice keeps `WES_CONV_TURNS` (6,
short spoken context where latency matters), while **Discord keeps
`WES_CONV_TURNS_DISCORD` (40) for a much longer `WES_CONV_TTL_DISCORD` (7 days)**
— a remote chat spans hours. Key behaviors:

- **Shared across the handoff**: escalated turns give Claude the same history
  gemma saw (and gemma later sees what Claude said) — without this, "explain that
  differently" breaks the moment a turn escalates.
- **Idle expiry**: the channel's TTL without a turn (voice 300s) clears the
  context, so this morning's chat doesn't color tonight's question.
- **Persisted across restarts**: each channel's window is written to
  `~/wes-pc/logs/conversations/<channel>.jsonl` (household content — on the PC,
  not the repo) and reloaded on startup (`load_conversations`), so a server
  restart no longer forgets mid-conversation. A window past its TTL (by file
  mtime) starts empty. This is Phase 0 of the memory architecture
  (`docs/memory-design.md`); the unified cross-channel semantic/episodic layer
  is the later phase.
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

## Token usage ledger (added 2026-07-05)

Every LLM call appends one row to `C:\Users\awarm\wes-pc\logs\usage.csv`
(`WES_USAGE_LOG` overrides): timestamp, model, call source (`router` /
`escalate` / `vlm` / `claude`), conversation channel, and tokens in/out —
Ollama reports counts on its final chunk, Claude in `response.usage`.
**`GET /usage`** (optional `?days=N`) rolls it up by model + source and prices
the tokens at claude-haiku-4-5 rates ($1/MTok in, $5/MTok out, cached
2026-06-24): for local models that's the estimated **saving** vs having sent
the call to the Claude API; for `claude` rows it's actual spend. Caveats: gemma
and Claude tokenize differently (rough estimate by design), zero-token rows
are dropped, and a ledger write failure never breaks a turn.

The sibling **turn log** (`turns.jsonl`, `GET /turns`) records the *content* of
each exchange — query, reply, tools run, escalated y/n — as a size-capped
rolling window; see `docs/observability.md`.

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
- `remember` / `forget` — durable memory (`MEMORY.md`), see the memory section.
- `lookup_hosts` — network registry (`hosts.yaml`).
- `nba_scores(team?, date?)` / `nba_player(player)` — live NBA data from ESPN's
  free hidden API (no key), via `pc/wes_nba.py`. Scores/status (quarter + clock),
  per-player points from the running box score, and dated results ("May 20th",
  "yesterday"). No team + no date defaults to the Nets; a dated query with no
  team lists all that day's games. Structured numbers only (no free prose).
- `nba_discussion` — what r/GoNets is talking about (recent post titles), via
  reddit's **RSS** feed (`pc/wes_nba.py`; reddit's JSON API 403s unauthenticated,
  RSS serves to a browser UA with no key). This is UNTRUSTED external free text
  and a prompt-injection vector, so it's guarded two ways: the tool result is
  wrapped in an adjacent `[UNTRUSTED …]` framing, and `system_prompt` appends
  `WEB_CONTENT_RULE` (both channels) — treat such content as quoted data only,
  never instructions, never a reason to remember/act. RSS is cached 5 min
  (reddit rate-limits rapid repeats). See ticket #027.

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
