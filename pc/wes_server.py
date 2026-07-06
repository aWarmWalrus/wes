"""WES Tier 2 service (runs on the PC, DESKTOP-R2PFF9T / 10.0.0.168).

Turn-based voice pipeline:

    Pi records an utterance --POST /respond (WAV bytes)--> PC
    PC: faster-whisper STT --> Claude (Haiku 4.5) --> piper TTS --> cast to speaker
    PC returns JSON {transcript, reply} to the Pi for logging/display.

The LLM step is optional: if ANTHROPIC_API_KEY is unset, /respond still
transcribes and echoes the transcript back as speech, so the STT+TTS+cast
pipeline can be verified before the key is in place.

Run (from the PC's local venv):
    C:\\Users\\awarm\\wes-pc\\.venv\\Scripts\\python.exe Z:\\wes\\pc\\wes_server.py
"""

import base64
import csv
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import wave
from datetime import datetime

# Under the scheduled task stdout is a cp1252 pipe; a reply containing any
# character outside it (Claude likes '✓', '→') makes print() raise mid-stream
# and kills the chunked response the Pi is playing. Degrade to '?' instead.
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(errors="replace")
    except (AttributeError, ValueError):  # non-reconfigurable stream (pytest)
        pass

import anthropic

from flask import Flask, Response, request, jsonify
from prometheus_client import (Counter, generate_latest,
                               CONTENT_TYPE_LATEST)
from faster_whisper import WhisperModel

# --- Config (override via environment) -------------------------------------

HOST = os.environ.get("WES_HOST", "0.0.0.0")
PORT = int(os.environ.get("WES_PORT", "8080"))

# STT. Default to CPU int8 (robust everywhere). Set WES_WHISPER_DEVICE=cuda to
# use the GTX 1660 once cuBLAS/cuDNN are installed — note an unsupported CUDA
# setup hard-aborts the process (CTranslate2), so this is opt-in, not auto.
WHISPER_MODEL = os.environ.get("WES_WHISPER_MODEL", "base.en")
WHISPER_DEVICE = os.environ.get("WES_WHISPER_DEVICE", "cpu")

# TTS (piper) + voice model, on the PC's local disk.
PC_HOME = os.path.expanduser("~")
PIPER_BIN = os.environ.get(
    "WES_PIPER_BIN", os.path.join(PC_HOME, "wes-pc", ".venv", "Scripts", "piper.exe")
)
VOICE_MODEL = os.environ.get(
    "WES_VOICE_MODEL", os.path.join(PC_HOME, "wes-pc", "voices", "en_GB-alan-medium.onnx")
)

# Output mode:
#   "return" — send the TTS WAV back to the Pi in the HTTP response; the Pi plays
#              it locally (e.g. over a Bluetooth speaker). Lowest latency.
#   "cast"   — cast to a Google Home / Nest device (higher latency: discovery +
#              fetch + buffer per reply).
OUTPUT_MODE = os.environ.get("WES_OUTPUT", "return")

# Cast target (only used when OUTPUT_MODE == "cast").
# Per project rule, NEVER use "Good gray" or "Matcha".
CAST_DEVICE = os.environ.get("WES_CAST_DEVICE", "Kitchen Display")
CAST_VOLUME = float(os.environ.get("WES_CAST_VOLUME", "0.5"))  # 5/10

# LLM. Backend "local" = Ollama chat (LOCAL_LLM_MODEL, tools + streaming);
# "claude" = Anthropic API. Local errors fall back to Claude when a key exists.
LLM_BACKEND = os.environ.get("WES_LLM", "claude")
LOCAL_LLM_MODEL = os.environ.get("WES_LLM_LOCAL_MODEL", "gemma4:e4b")
# Smart routing: give the local model an escalate_to_claude function so IT
# decides when a query is beyond it (WES_ESCALATE=0 disables).
ESCALATE = os.environ.get("WES_ESCALATE", "1") == "1"
# Where escalations go: an Ollama model name (e.g. "gemma4:12b" — answered
# locally with thinking enabled) or empty for Claude (needs the API key).
# The tool the router sees keeps its prompt-tuned name/description either
# way — its semantics ("hand off to the much smarter model") don't change.
ESCALATE_MODEL = os.environ.get("WES_ESCALATE_MODEL", "")
# Spoken by the SERVER (not the model) the moment an escalation fires, so the
# ~2-3s Claude spin-up isn't dead air. Must end with a sentence terminator +
# space so the TTS splitter flushes it immediately. Empty string disables.
ESCALATE_ACK = os.environ.get(
    "WES_ESCALATE_ACK", "Good question — let me think about that. ")
ANTHROPIC_MODEL = os.environ.get("WES_LLM_MODEL", "claude-haiku-4-5")
SYSTEM_PROMPT = os.environ.get(
    "WES_SYSTEM_PROMPT",
    "You are Jarvis, a voice assistant running on a Raspberry Pi. Your replies "
    "are read aloud by a text-to-speech engine, so keep them short and "
    "conversational — one or two sentences — in plain spoken English: no "
    "markdown, bullet points, headings, asterisks, emoji, or other symbols, and "
    "say numbers, times, and units the way a person would say them out loud. "
    "The user's words reach you through speech recognition, so if a word looks "
    "slightly wrong, interpret it charitably from context instead of taking it "
    "literally. "
    "You have tools to check the Pi's live status (temperature, memory, "
    "etc.), the date/time, and recent logs — use them when the question calls for "
    "current information rather than guessing. You are also a general assistant: "
    "answer everyday knowledge questions confidently from what you know.",
)

# The base prompt is written for the voice loop; text frontends (Discord)
# override its speech assumptions so the model doesn't talk about "voice
# commands" to someone typing on their phone.
TEXT_CHANNEL_NOTE = (
    " Correction for this conversation: the user is TYPING to you over a text "
    "chat (Discord), probably away from home — no microphone, speech "
    "recognition, or text-to-speech is involved, so never mention voice or "
    "speaking. Their words arrive exactly as typed. Keep replies short and "
    "conversational; digits and simple formatting are fine in text."
)


def system_prompt(channel="voice"):
    """The system prompt for a turn: base + channel framing + live scene."""
    note = "" if channel == "voice" else TEXT_CHANNEL_NOTE
    return SYSTEM_PROMPT + note + _scene_context()


# Framing for a proactive notification (an alert, later a scheduled action):
# the user did NOT ask anything, so Jarvis must not answer as if replying.
ANNOUNCE_FRAMING = (
    "[WES system monitoring event — the user did NOT send you a message. "
    "Proactively notify them about the situation below in your own voice: say "
    "plainly what happened, what it means, and why it matters, and mention an "
    "obvious next step if there is one. One or two sentences. Do not say the "
    "user asked; do not invent any detail beyond what is given.]\n\n")

# --- Tools (Pi introspection) ----------------------------------------------
TOOLS_ENABLED = os.environ.get("WES_TOOLS", "1") == "1"
PI_STATE_URL = os.environ.get("WES_PI_STATE_URL", "http://10.0.0.79:8090")
MAX_TOOL_ROUNDS = 4

# Local vision-language model (Gemma via Ollama) for rich scene descriptions.
OLLAMA_URL = os.environ.get("WES_OLLAMA_URL", "http://127.0.0.1:11434")
VLM_MODEL = os.environ.get("WES_VLM_MODEL", "gemma3:4b")
VLM_PROMPT = ("Describe what you see in this image in one or two natural, "
              "conversational sentences, as an assistant describing the view.")
# A scene description prefetched at wake-word time is reused within this window.
SCENE_TTL = float(os.environ.get("WES_SCENE_TTL", "20"))
_scene_lock = threading.Lock()
# desc: Gemma's description. faces: face-rec results ([] = ran, nobody in frame;
# None = no recognition data). faces_ts is set the moment identities arrive (before
# Gemma finishes) so the turn's system context can use them immediately.
_scene_cache = {"desc": None, "ts": 0.0, "faces": None, "faces_ts": 0.0}

TOOLS = [
    {
        "name": "get_system_status",
        "description": (
            "Get the Raspberry Pi's live system status: CPU temperature (°C), "
            "throttling state, load average, memory, disk, uptime, and whether the "
            "Bluetooth speaker is connected. Use when asked about the Pi's health, "
            "temperature, memory/resources, or how it's doing."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_datetime",
        "description": "Get the current date and time. Use when asked the time or day.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "look",
        "description": (
            "Look through the camera and detect what objects are currently visible, "
            "using on-device computer vision. Returns a list of detected objects with "
            "their rough position (left/center/right) and size. Use when asked what "
            "you see, who/what is there, or to describe the view."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "describe_scene",
        "description": (
            "Look through the camera: rich natural-language description of the scene "
            "PLUS face recognition — returns who is in frame by name (with position "
            "and clothing) and what they're doing. Use whenever asked what you see, "
            "or anything about a specific person (how they look, what they're doing, "
            "whether they're present). For a quick object list only, use 'look'."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "read_pi_log",
        "description": (
            "Read recent log lines from the Pi to investigate errors or recent "
            "activity. Pick the relevant service."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "service": {
                    "type": "string",
                    "enum": ["bluetooth", "wireplumber", "pipewire", "kernel"],
                },
                "lines": {"type": "integer", "description": "recent lines (max 50)"},
            },
            "required": ["service"],
        },
    },
]


def _ollama_tools():
    """TOOLS (Anthropic schema) -> Ollama/OpenAI function-calling schema."""
    return [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t["description"],
                "parameters": t["input_schema"],
            },
        }
        for t in TOOLS
    ]


# Routing tool for the local backend only: the local model calls this to hand
# a hard query off to Claude. Never in TOOLS (Claude must not see it).
ESCALATE_TOOL = {
    "type": "function",
    "function": {
        "name": "escalate_to_claude",
        "description": (
            "Hand this question off to Claude, a much more capable cloud AI, and "
            "let it answer instead of you. Use when the question needs deep or "
            "multi-step reasoning, non-trivial math or code, specialized or "
            "detailed knowledge, or careful nuanced judgment — anything a small "
            "local model is likely to get wrong. Do NOT use for everyday "
            "conversation, simple facts, or anything your other tools already "
            "cover (time, camera, faces, Pi status, logs). "
            "Call it IMMEDIATELY as your only output — no reply text before or "
            "alongside it. The handoff is invisible to the user: never announce "
            "it, never mention Claude or asking for help, never tell the user to "
            "ask someone else."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "reason": {"type": "string",
                           "description": "one short phrase: why this needs Claude"},
            },
            "required": [],
        },
    },
}


def _local_toolset():
    """Tools for the router: the shared TOOLS plus, when an escalation target
    exists (a local deep model, or Claude with a key), the escalation function
    for smart routing."""
    tools = _ollama_tools() if TOOLS_ENABLED else []
    if ESCALATE and (ESCALATE_MODEL or os.environ.get("ANTHROPIC_API_KEY")):
        tools = tools + [ESCALATE_TOOL]
    return tools


def _pi_get(path, params=None):
    url = f"{PI_STATE_URL}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=12) as r:
        return json.loads(r.read().decode())


def _vlm_prompt(identities):
    """Base describe prompt, augmented with any recognized people."""
    if isinstance(identities, dict):
        identities = [identities]
    if not isinstance(identities, list):
        identities = []
    known = [f for f in identities
             if isinstance(f, dict) and f.get("name") and f["name"] != "unknown"]
    if known:
        who = ", ".join(
            f"{f['name']} (wearing {f.get('clothing', 'unknown')}, {f['position']})"
            for f in known
        )
        return (f"Face recognition has identified these people in the image, with the "
                f"color they're wearing to help tell them apart: {who}. Use the "
                f"clothing colors to match each name to the right person, especially "
                f"if people are close together. In two or three conversational "
                f"sentences, describe each identified person BY NAME — their "
                f"appearance, expression, posture, and what they're doing — then the "
                f"surroundings briefly. Never say 'a person' or 'a man/woman' for "
                f"someone who has a name.")
    return VLM_PROMPT


def _gemma_describe(jpeg, prompt=None):
    """Run the local Gemma VLM on a JPEG and return the description string."""
    import base64

    payload = json.dumps({
        "model": VLM_MODEL,
        "prompt": prompt or VLM_PROMPT,
        "images": [base64.b64encode(jpeg).decode()],
        "stream": False,
        "keep_alive": -1,  # keep the model resident so it never cold-loads
    }).encode()
    req = urllib.request.Request(
        f"{OLLAMA_URL}/api/generate", data=payload,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        data = json.loads(r.read().decode())
    record_usage(VLM_MODEL, "vlm", "scene",
                 data.get("prompt_eval_count"), data.get("eval_count"))
    return data.get("response", "").strip() or "(no description)"


def prefetch_scene(jpeg, identities=None):
    """Describe a frame captured at wake-word time (with recognized identities
    woven in) and cache it, so describe_scene returns instantly."""
    if isinstance(identities, dict):
        identities = [identities]
    if not isinstance(identities, list):
        identities = []
    # Identities are known now — publish them immediately so the turn's system
    # context can say who's in frame even while Gemma is still describing.
    with _scene_lock:
        _scene_cache["faces"] = identities
        _scene_cache["faces_ts"] = time.time()
    try:
        desc = _gemma_describe(jpeg, _vlm_prompt(identities))
        with _scene_lock:
            _scene_cache["desc"] = desc
            _scene_cache["ts"] = time.time()
        names = [f.get("name") for f in identities if isinstance(f, dict)]
        print(f"[vision] prefetched scene {names}: {desc[:70]!r}", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"[vision] prefetch error: {e!r}", flush=True)


def _face_summary(faces):
    """Structured identity summary for tool output / Claude context."""
    if faces is None:
        return {"recognition_ran": False, "people": []}
    people = [
        {"name": f.get("name", "unknown"), "position": f.get("position", "?"),
         "clothing": f.get("clothing", "unknown")}
        for f in faces if isinstance(f, dict)
    ]
    return {"recognition_ran": True, "people": people}


def describe_scene():
    """Rich scene description WITH face identity.

    Cache hit: the wake-word prefetch already ran face-rec + Gemma. Miss: capture
    via the Pi's /scene (face-rec included) and describe with an identity-aware
    prompt. Returns {"description": str, **_face_summary(...)}."""
    with _scene_lock:
        if _scene_cache["desc"] and time.time() - _scene_cache["ts"] < SCENE_TTL:
            print("[vision] describe_scene: cache HIT", flush=True)
            return {"description": _scene_cache["desc"],
                    **_face_summary(_scene_cache["faces"])}
    print("[vision] describe_scene: miss — capturing now (with face-rec)", flush=True)
    faces = None
    jpeg = b""
    try:  # preferred: one capture with recognition
        with urllib.request.urlopen(f"{PI_STATE_URL}/scene", timeout=30) as r:
            data = json.loads(r.read().decode())
        if "error" not in data:
            faces = data.get("faces", [])
            jpeg = base64.b64decode(data.get("jpeg_b64") or "")
    except Exception as e:  # noqa: BLE001
        print(f"[vision] /scene failed ({e!r}); falling back to /frame", flush=True)
    if not jpeg:  # fallback: bare frame, no identity
        with urllib.request.urlopen(f"{PI_STATE_URL}/frame", timeout=15) as r:
            jpeg = r.read()
    if not jpeg:
        return {"description": "I couldn't capture an image from the camera.",
                **_face_summary(None)}
    desc = _gemma_describe(jpeg, _vlm_prompt(faces))
    with _scene_lock:
        _scene_cache.update(desc=desc, ts=time.time(),
                            faces=faces, faces_ts=time.time())
    return {"description": desc, **_face_summary(faces)}


def _scene_context():
    """System-prompt addendum: who face recognition currently sees.

    Injected into every Claude turn so questions about a person ("how does
    charlie look?") are answered assuming the recognized person IS in frame —
    and 'I don't see them' is said only when recognition genuinely didn't find
    them. Empty string when there's no fresh recognition data."""
    with _scene_lock:
        faces = _scene_cache["faces"]
        fresh = faces is not None and time.time() - _scene_cache["faces_ts"] < SCENE_TTL
    if not fresh:
        return ""
    known = [f for f in faces
             if isinstance(f, dict) and f.get("name") and f["name"] != "unknown"]
    unknown_n = sum(1 for f in faces if isinstance(f, dict)) - len(known)
    if known:
        who = ", ".join(
            f"{f['name']} ({f.get('position', '?')}, wearing {f.get('clothing', 'unknown')})"
            for f in known
        )
        extra = f" plus {unknown_n} unrecognized person(s)" if unknown_n else ""
        seen = f"{who}{extra}"
    elif unknown_n:
        seen = f"{unknown_n} person(s), but none match enrolled faces"
    else:
        seen = "no people"
    return (
        f"\n\nLive camera face recognition (moments ago): currently in frame: {seen}. "
        "Treat this as ground truth for who is present. If asked about a person "
        "listed here, they ARE in view — answer about them directly (use "
        "describe_scene for visual detail). Only say you can't find someone if "
        "they are NOT listed here."
    )


def run_tool(name, tool_input):
    """Execute a tool and return a string result for Claude."""
    try:
        if name == "get_system_status":
            return json.dumps(_pi_get("/state"))
        if name == "describe_scene":
            return json.dumps(describe_scene())
        if name == "get_datetime":
            # spoken-friendly: no ISO dates, 24h clocks, or seconds — the
            # model repeats this string near-verbatim into TTS
            now = datetime.now().astimezone()
            return now.strftime("%A, %B %d, %Y, %I:%M %p").replace(" 0", " ")
        if name == "look":
            return json.dumps(_pi_get("/look"))
        if name == "read_pi_log":
            data = _pi_get(
                "/logs",
                {"service": tool_input.get("service", ""),
                 "n": tool_input.get("lines", 20)},
            )
            return data.get("log", "(no log)")
        return f"unknown tool: {name}"
    except Exception as e:  # noqa: BLE001
        return f"tool error: {e}"

app = Flask(__name__)

# --- Lazy singletons --------------------------------------------------------

_whisper = None
_anthropic = None


def get_whisper():
    global _whisper
    if _whisper is None:
        if WHISPER_DEVICE == "cuda":
            _whisper = WhisperModel(WHISPER_MODEL, device="cuda", compute_type="float16")
            app.logger.info("Whisper on CUDA (float16)")
        else:
            _whisper = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
            app.logger.info("Whisper on CPU (int8)")
    return _whisper


def get_anthropic():
    """Return an Anthropic client, or None if no credentials are configured."""
    global _anthropic
    if _anthropic is None:
        if not os.environ.get("ANTHROPIC_API_KEY"):
            return None
        _anthropic = anthropic.Anthropic()
    return _anthropic


# --- LLM rate-limit budget --------------------------------------------------
# The org has a low RPM cap (default assume 5/min). Speculation must not starve
# the real finalize request, so we reserve slots for it and gate speculation.
_RATE_LIMIT_RPM = int(os.environ.get("WES_LLM_RPM", "5"))
_SPEC_RESERVE = int(os.environ.get("WES_SPEC_RESERVE", "2"))  # slots kept for real turns
_rate_lock = threading.Lock()
_rate_calls = []  # timestamps of recent Claude calls


def _record_llm_call():
    with _rate_lock:
        _rate_calls.append(time.time())


def _llm_calls_last_min():
    now = time.time()
    with _rate_lock:
        while _rate_calls and now - _rate_calls[0] > 60.0:
            _rate_calls.pop(0)
        return len(_rate_calls)


def _spec_budget_ok():
    """True if there's spare budget to speculate (keeping a reserve for real turns)."""
    if LLM_BACKEND == "local":
        return True  # local inference has no API rate limit
    return _llm_calls_last_min() < max(0, _RATE_LIMIT_RPM - _SPEC_RESERVE)


# --- Pipeline steps ---------------------------------------------------------


# Contextual biasing: whisper's initial_prompt reads to the decoder like the
# transcript-so-far, so ambiguous audio resolves toward in-context words —
# proper nouns especially. The lexicon is this house's vocabulary (devices,
# hardware, household names); launcher-overridable, empty string disables.
STT_LEXICON = os.environ.get(
    "WES_STT_LEXICON",
    "Jarvis is a voice assistant running on a Raspberry Pi with a Hailo "
    "accelerator. It can control the Hue lights and the ecobee thermostat, "
    "knows the speakers Matcha, Good gray, Stevie, and the JBL, and knows "
    "Charlie, Cindy, Kaia, and Ellis.",
)


def stt_bias_prompt():
    """Whisper initial_prompt: the domain lexicon plus the tail of the live
    conversation (same idea as Google boosting your on-screen entities).
    Kept short — an overlong prompt makes whisper hallucinate prompt words
    into marginal audio (the robust-silence eval case is the tripwire)."""
    tail = " ".join(m["content"] for m in conversation_context()[-2:])
    return (STT_LEXICON + " " + tail[:300]).strip()


def transcribe(wav_bytes):
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        f.write(wav_bytes)
        path = f.name
    try:
        segments, _ = get_whisper().transcribe(
            path, language="en", initial_prompt=stt_bias_prompt() or None)
        return " ".join(s.text.strip() for s in segments).strip()
    finally:
        os.remove(path)


RATE_LIMIT_REPLY = "Sorry, I'm being rate limited right now. Try again in a moment."


def _ollama_chat(messages, tools=None, stream=False, max_tokens=512, timeout=120,
                 model=None, think=False):
    """POST to Ollama /api/chat. Returns the response object (caller iterates
    lines when stream=True, or json-decodes .read() when stream=False).
    Thinking deltas arrive in message.thinking, separate from content, so
    they are never spoken — callers only read message.content."""
    body = {
        "model": model or LOCAL_LLM_MODEL,
        "messages": messages,
        "stream": stream,
        "think": think,
        "keep_alive": -1,
        "options": {"num_predict": max_tokens},
    }
    if tools:
        body["tools"] = tools
    req = urllib.request.Request(
        f"{OLLAMA_URL}/api/chat", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    return urllib.request.urlopen(req, timeout=timeout)


# --- Conversation memory ----------------------------------------------------
# LiveKit ChatContext-style sliding window, keyed by channel: "voice" is the
# house conversation (one house, one mic); remote text frontends (Discord) get
# their own channel so a chat from away doesn't clobber the in-house context.
# The last CONV_TURNS exchanges are replayed to whichever backend answers —
# including across the escalation handoff, so Claude sees what gemma said.
# Each channel goes idle-empty after CONV_TTL seconds of silence.
CONV_TURNS = int(os.environ.get("WES_CONV_TURNS", "6"))
CONV_TTL = float(os.environ.get("WES_CONV_TTL", "300"))
_conv_lock = threading.Lock()
_convs = {}      # channel -> [{"role": "user"|"assistant", "content": str}]
_conv_last = {}  # channel -> last activity time


def conversation_context(channel="voice"):
    """Recent exchanges for the LLM, or [] once the conversation went idle."""
    with _conv_lock:
        conv = _convs.setdefault(channel, [])
        if time.time() - _conv_last.get(channel, 0.0) > CONV_TTL:
            conv.clear()
        return list(conv)


def record_turn(transcript, reply, channel="voice"):
    """Append one exchange, keeping the last CONV_TURNS exchanges.
    Empty transcripts (silence) and empty replies are not memory."""
    if not (transcript and transcript.strip() and reply and reply.strip()):
        return
    log_turn(transcript, reply, channel)
    with _conv_lock:
        conv = _convs.setdefault(channel, [])
        if time.time() - _conv_last.get(channel, 0.0) > CONV_TTL:
            conv.clear()
        conv.append({"role": "user", "content": transcript})
        conv.append({"role": "assistant", "content": reply})
        del conv[:-2 * CONV_TURNS]
        _conv_last[channel] = time.time()


def reset_conversation(channel=None):
    """Clear one channel's memory, or every channel's when channel is None."""
    with _conv_lock:
        convs = [_convs.get(channel, [])] if channel else list(_convs.values())
        n = sum(len(c) // 2 for c in convs)
        for c in convs:
            c.clear()
        return n


# --- Token usage ledger -------------------------------------------------------
# Every LLM call (local and Claude) appends one CSV row so we can track local
# token volume and estimate what it "saved" vs sending the same traffic to the
# Claude API. GET /usage aggregates it.
USAGE_LOG = os.environ.get(
    "WES_USAGE_LOG",
    os.path.join(os.path.expanduser("~"), "wes-pc", "logs", "usage.csv"))
USAGE_FIELDS = ["ts", "model", "source", "channel", "in_tokens", "out_tokens"]
# The counterfactual price: claude-haiku-4-5 (the model WES otherwise calls),
# USD per million tokens — cached from Anthropic's pricing table 2026-06-24;
# update if Anthropic reprices. Local (gemma) token counts come from a
# different tokenizer than Claude's, so the estimate is rough by nature.
HAIKU_USD_PER_MTOK_IN = 1.00
HAIKU_USD_PER_MTOK_OUT = 5.00
_usage_lock = threading.Lock()

# The same ledger data, exposed live for Prometheus (GET /metrics, scraped by
# the Pi — docs/observability.md). Counters reset on server restart; rate()/
# increase() in Grafana handle that. The CSV stays the all-time source of
# truth for GET /usage.
TOKENS_TOTAL = Counter(
    "wes_llm_tokens_total", "LLM tokens processed, by call type",
    ["direction", "model", "source", "channel"])
CALLS_TOTAL = Counter(
    "wes_llm_calls_total", "LLM calls made, by call type",
    ["model", "source", "channel"])


def record_usage(model, source, channel, in_tokens, out_tokens):
    """Append one LLM call to the ledger. Zero-token rows are dropped (the
    backend didn't report usage) and failures never break a turn."""
    if not (in_tokens or out_tokens):
        return
    CALLS_TOTAL.labels(model, source, channel).inc()
    if in_tokens:
        TOKENS_TOTAL.labels("in", model, source, channel).inc(int(in_tokens))
    if out_tokens:
        TOKENS_TOTAL.labels("out", model, source, channel).inc(int(out_tokens))
    try:
        with _usage_lock:
            os.makedirs(os.path.dirname(USAGE_LOG), exist_ok=True)
            new = not os.path.exists(USAGE_LOG)
            with open(USAGE_LOG, "a", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                if new:
                    w.writerow(USAGE_FIELDS)
                w.writerow([time.strftime("%Y-%m-%d %H:%M:%S"), model, source,
                            channel, int(in_tokens or 0), int(out_tokens or 0)])
    except OSError as e:  # noqa: BLE001
        print(f"[usage] ledger write failed: {e}", flush=True)


def usage_summary(days=None):
    """Aggregate the ledger by (model, source): calls, tokens, and USD at
    Haiku rates — for local models that's the estimated *saving* vs having
    sent the same call to the Claude API; for claude rows it's actual spend."""
    cutoff = time.time() - days * 86400 if days else None
    groups = {}
    try:
        with open(USAGE_LOG, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                if cutoff:
                    try:
                        ts = time.mktime(time.strptime(r["ts"], "%Y-%m-%d %H:%M:%S"))
                        if ts < cutoff:
                            continue
                    except (ValueError, KeyError):
                        pass
                key = (r.get("model", "?"), r.get("source", "?"))
                g = groups.setdefault(key, {"calls": 0, "in_tokens": 0, "out_tokens": 0})
                g["calls"] += 1
                g["in_tokens"] += int(r.get("in_tokens") or 0)
                g["out_tokens"] += int(r.get("out_tokens") or 0)
    except FileNotFoundError:
        pass

    rows, saved, spent = [], 0.0, 0.0
    for (model, source), g in sorted(groups.items()):
        usd = (g["in_tokens"] * HAIKU_USD_PER_MTOK_IN
               + g["out_tokens"] * HAIKU_USD_PER_MTOK_OUT) / 1e6
        local = not model.startswith("claude")
        rows.append({"model": model, "source": source, "local": local,
                     "usd_at_haiku_rates": round(usd, 4), **g})
        if local:
            saved += usd
        else:
            spent += usd
    return {
        "days": days,
        "by_call_site": rows,
        "local_saved_usd_estimate": round(saved, 4),
        "claude_spent_usd": round(spent, 4),
        "pricing_basis": "claude-haiku-4-5: $1.00/MTok in, $5.00/MTok out "
                         "(cached 2026-06-24)",
        "note": "'saved' prices local tokens at Haiku rates; gemma and Claude "
                "tokenize differently, so treat it as a rough estimate",
    }


# --- Turn log -----------------------------------------------------------------
# One JSONL record per completed exchange, any channel: what was said, the
# reply, which tools ran, and whether it escalated. Served by GET /turns and
# rendered as a "Recent turns" table in Grafana (docs/observability.md).
# Unlike the usage ledger this stores CONTENT — transcripts of the house — so
# it is a size-capped rolling window, not an append-forever file.
TURNS_LOG = os.environ.get(
    "WES_TURNS_LOG",
    os.path.join(os.path.expanduser("~"), "wes-pc", "logs", "turns.jsonl"))
TURNS_MAX = int(os.environ.get("WES_TURNS_MAX", "2000"))
_TURNS_TRIM_BYTES = 4_000_000  # trim to the last TURNS_MAX lines past this
_turns_lock = threading.Lock()
# Tool calls happen deep in the backend loops while the turn is recorded at the
# route level; a thread-local notepad connects them (one turn's LLM work runs
# entirely on one thread — speculation caching only applies with tools off).
_turn_notes = threading.local()


def _turn_begin():
    """Reset this thread's tool/escalation notes at the start of a turn."""
    _turn_notes.tools = []
    _turn_notes.escalated = False


def _note_tool(name):
    tools = getattr(_turn_notes, "tools", None)
    if tools is not None:
        tools.append(name)


def _note_escalation():
    if getattr(_turn_notes, "tools", None) is not None:
        _turn_notes.escalated = True


def log_turn(transcript, reply, channel="voice"):
    """Append one exchange (+ this thread's notes) to the turn log. Consumes
    the notes; trims the file to TURNS_MAX lines; never breaks a turn."""
    rec = {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "channel": channel,
        "transcript": transcript,
        "reply": reply,
        "tools": list(getattr(_turn_notes, "tools", None) or []),
        "escalated": bool(getattr(_turn_notes, "escalated", False)),
    }
    _turn_begin()  # notes are per-turn — never leak into this thread's next one
    try:
        with _turns_lock:
            os.makedirs(os.path.dirname(TURNS_LOG), exist_ok=True)
            with open(TURNS_LOG, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            if os.path.getsize(TURNS_LOG) > _TURNS_TRIM_BYTES:
                with open(TURNS_LOG, encoding="utf-8") as f:
                    tail = f.readlines()[-TURNS_MAX:]
                with open(TURNS_LOG, "w", encoding="utf-8") as f:
                    f.writelines(tail)
    except OSError as e:
        print(f"[turns] log write failed: {e}", flush=True)


def recent_turns(n=20, channel=None):
    """The last n logged exchanges, newest first, optionally one channel's."""
    try:
        with _turns_lock, open(TURNS_LOG, encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return []
    out = []
    for line in reversed(lines):
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if channel and rec.get("channel") != channel:
            continue
        out.append(rec)
        if len(out) >= n:
            break
    return out


# LiveKit-style interruption tag: when the client aborts playback (barge-in),
# the partial reply IS what the user heard — remember it, but tell the model
# where it was cut off so the follow-up turn can pivot naturally.
INTERRUPT_TAG = " [reply interrupted by the user]"


def record_spoken_turn(transcript, reply, completed):
    """Record one exchange; tag the reply if the stream was aborted mid-way."""
    if reply and not completed:
        reply = reply.rstrip() + INTERRUPT_TAG
    record_turn(transcript, reply)


# Buffered-mode marker: nothing yielded so far reached the user — discard it.
# Lets a buffered caller retract a spoken escalation announcement ("I think
# you should ask Claude...") and replace it with the deep tier's real answer.
RESET = object()


def _think_local(transcript, channel="voice"):
    """One-shot local reply — same tool loop as streaming, joined. Without tools
    gemma4 hallucinates tool output (fake times, literal '{tool_output}').
    Runs buffered: no text reaches the user until the reply is complete, so a
    late escalation can still cleanly replace everything said before it."""
    parts = []
    for delta in _stream_local(transcript, channel=channel, buffered=True):
        if delta is RESET:
            parts.clear()
        else:
            parts.append(delta)
    return "".join(parts).strip()


def _stream_escalation(transcript, channel="voice"):
    """Route an escalated (hard) query to the configured deep backend:
    the local ESCALATE_MODEL with thinking, or Claude."""
    if ESCALATE_MODEL:
        yield from _stream_local(transcript, channel=channel, deep=True)
    else:
        yield from _stream_claude(transcript, channel=channel)


def _stream_local(transcript, channel="voice", deep=False, buffered=False):
    """Stream a local reply with the same tool loop the Claude path runs.
    Yields text deltas; runs requested tools between rounds. deep=True is the
    escalation tier: ESCALATE_MODEL with thinking enabled (thinking streams in
    message.thinking, which we never read, so it is never spoken), the shared
    tools but no escalate function, and a larger token budget to cover the
    thinking. buffered=True means the caller holds the reply until complete
    (nothing has been spoken), so a mid-reply escalation can yield RESET to
    retract the announcement instead of being suppressed."""
    messages = (
        [{"role": "system", "content": system_prompt(channel)}]
        + conversation_context(channel)
        + [{"role": "user", "content": transcript}]
    )
    tools = (_ollama_tools() if TOOLS_ENABLED else []) if deep else _local_toolset()
    model = ESCALATE_MODEL if deep else None
    max_tokens = 2048 if deep else 512
    yielded = False
    for _ in range(MAX_TOOL_ROUNDS):
        content_parts, tool_calls = [], []
        with _ollama_chat(messages, tools=tools, stream=True,
                          model=model, think=deep, max_tokens=max_tokens) as r:
            for line in r:
                chunk = json.loads(line)
                msg = chunk.get("message") or {}
                if msg.get("content"):
                    content_parts.append(msg["content"])
                    yielded = True
                    yield msg["content"]
                tool_calls.extend(msg.get("tool_calls") or [])
                if chunk.get("done"):
                    # Ollama reports token usage on the final chunk.
                    record_usage(model or LOCAL_LLM_MODEL,
                                 "escalate" if deep else "router", channel,
                                 chunk.get("prompt_eval_count"),
                                 chunk.get("eval_count"))
                    break
        if not tool_calls:
            return  # final answer delivered
        messages.append({
            "role": "assistant",
            "content": "".join(content_parts),
            "tool_calls": tool_calls,
        })
        for tc in tool_calls:
            fn = tc.get("function") or {}
            name, args = fn.get("name", ""), fn.get("arguments") or {}
            if name == "escalate_to_claude":
                if not deep and (not yielded or buffered):
                    target = ESCALATE_MODEL or "Claude"
                    print(f"[route] escalating to {target}: "
                          f"{args.get('reason', '?')}", flush=True)
                    _note_escalation()
                    if buffered:
                        # Nothing reached the user — retract any announcement
                        # ("ask Claude..."); no ack needed, there's no dead
                        # air to mask when the reply arrives whole.
                        if yielded:
                            yield RESET
                    elif ESCALATE_ACK:
                        yield ESCALATE_ACK  # masks the deep tier's spin-up
                    yield from _stream_escalation(transcript, channel=channel)
                    return
                # Already mid-reply — a handoff now would double-speak.
                messages.append({
                    "role": "tool", "tool_name": name,
                    "content": ("Escalation unavailable mid-reply — answer the "
                                "question yourself, concisely."),
                })
                continue
            print(f"[tool] {name}({args})", flush=True)
            _note_tool(name)
            messages.append({
                "role": "tool",
                "tool_name": name,
                "content": run_tool(name, args),
            })


def think(transcript, channel="voice"):
    """Get a reply from the configured LLM backend."""
    _turn_begin()
    if LLM_BACKEND == "local":
        try:
            return _think_local(transcript, channel=channel)
        except Exception as e:  # noqa: BLE001
            print(f"[llm] local error: {e!r} -> falling back to Claude", flush=True)
    return _think_claude(transcript, channel=channel)


def announce(event, channel="discord"):
    """Have Jarvis phrase an internal system event as a proactive, natural
    message to the owner, then record it into the channel's conversation
    memory so a follow-up ("what was that?") has context. Returns the text.

    The recorded user-side is a compact '[system event] ...' marker, not the
    verbose framing instructions — future context sees what triggered Jarvis,
    not the meta-prompt."""
    reply = think(ANNOUNCE_FRAMING + event, channel=channel)
    record_turn(f"[system event] {event}", reply, channel=channel)
    return reply


def _think_claude(transcript, channel="voice"):
    """Get a reply from Claude, or echo the transcript if no API key."""
    client = get_anthropic()
    if client is None:
        return f"I heard: {transcript}" if transcript else "I didn't catch that."
    _record_llm_call()
    try:
        resp = client.messages.create(
            model=ANTHROPIC_MODEL,
            max_tokens=256,
            system=system_prompt(channel),
            messages=conversation_context(channel)
            + [{"role": "user", "content": transcript}],
        )
        record_usage(ANTHROPIC_MODEL, "claude", channel,
                     resp.usage.input_tokens, resp.usage.output_tokens)
        return "".join(b.text for b in resp.content if b.type == "text").strip()
    except anthropic.RateLimitError:
        return RATE_LIMIT_REPLY
    except Exception as e:  # noqa: BLE001
        print(f"[llm] error: {e!r}", flush=True)
        return "Sorry, I ran into an error. Please try again."


def synthesize(text, out_path):
    subprocess.run(
        [PIPER_BIN, "-m", VOICE_MODEL, "-f", out_path],
        input=tts_clean(text).encode("utf-8"),
        check=True,
    )


# --- Streaming TTS (in-process piper) --------------------------------------

_voice = None


def get_voice():
    """Load the piper voice once (avoids per-sentence model reload)."""
    global _voice
    if _voice is None:
        from piper import PiperVoice

        _voice = PiperVoice.load(VOICE_MODEL)
        app.logger.info("piper voice loaded (streaming)")
    return _voice


def stream_reply(transcript):
    """Yield the reply text in deltas from the configured LLM backend."""
    _turn_begin()
    if not transcript:
        yield "Sorry, I didn't catch that."
        return
    if LLM_BACKEND == "local":
        yielded = False
        try:
            for delta in _stream_local(transcript):
                yielded = True
                yield delta
            return
        except Exception as e:  # noqa: BLE001
            print(f"[llm] local stream error: {e!r}", flush=True)
            if yielded:
                # Mid-reply failure — can't cleanly restart on another backend.
                yield " Sorry, I lost my train of thought there."
                return
            print("[llm] falling back to Claude", flush=True)
    yield from _stream_claude(transcript)


def _stream_claude(transcript, channel="voice"):
    """Yield the reply text in deltas as Claude streams it."""
    client = get_anthropic()
    if client is None:
        yield f"I heard: {transcript}"
        return

    # History rides along on escalation, so Claude sees what gemma already said
    messages = conversation_context(channel) + [{"role": "user", "content": transcript}]
    tools = TOOLS if TOOLS_ENABLED else []
    system = system_prompt(channel)  # channel framing + who's in frame right now

    for _ in range(MAX_TOOL_ROUNDS):
        _record_llm_call()
        try:
            with client.messages.stream(
                model=ANTHROPIC_MODEL,
                max_tokens=512,
                system=system,
                messages=messages,
                tools=tools,
            ) as stream:
                for text in stream.text_stream:
                    yield text
                final = stream.get_final_message()
            record_usage(ANTHROPIC_MODEL, "claude", channel,
                         final.usage.input_tokens, final.usage.output_tokens)
        except anthropic.RateLimitError:
            yield RATE_LIMIT_REPLY
            return
        except Exception as e:  # noqa: BLE001
            print(f"[llm] stream error: {e!r}", flush=True)
            yield "Sorry, I ran into an error. Please try again."
            return

        messages.append({"role": "assistant", "content": final.content})
        if final.stop_reason != "tool_use":
            return  # final answer delivered

        # Run the requested tools and feed results back.
        tool_results = []
        for block in final.content:
            if block.type == "tool_use":
                print(f"[tool] {block.name}({block.input})", flush=True)
                _note_tool(block.name)
                result = run_tool(block.name, block.input)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result,
                })
        messages.append({"role": "user", "content": tool_results})


# Split off a complete sentence when a terminator is followed by whitespace.
_SENTENCE = re.compile(r"[.!?]\s")


def next_sentence(buf):
    m = _SENTENCE.search(buf)
    if m:
        return buf[: m.start() + 1], buf[m.end():]
    return None, buf


# The prompt asks for plain spoken English, but models still slip markdown in
# (escalated Claude replies especially). Strip it before piper reads "asterisk
# asterisk" aloud. Applied to every sentence at both TTS entry points.
_TTS_STRIP = [
    (re.compile(r"```[a-zA-Z]*"), " "),                 # code fences
    (re.compile(r"^\s{0,3}#{1,6}\s+", re.M), ""),       # headings
    (re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+", re.M), ""),  # list markers
    (re.compile(r"\[([^\]]+)\]\([^)]*\)"), r"\1"),      # [text](url) -> text
    (re.compile(r"[*_`~#|]+"), ""),                     # emphasis/code/table
    (re.compile(r"[→⇒]"), " to "),
    (re.compile(r"[✓✔]"), ""),
    (re.compile(r"[ \t]{2,}"), " "),
]


def tts_clean(text):
    """Reduce model output to plain speakable English for piper."""
    for pat, repl in _TTS_STRIP:
        text = pat.sub(repl, text)
    return text.strip()


# --- Speculative prefetch ---------------------------------------------------
# During recording the Pi POSTs partial audio to /speculate every ~2s. We run
# Claude speculatively on each partial transcript and cache the reply keyed by
# the normalized transcript. On finalize (/respond_stream), a matching cached
# reply skips the Claude call entirely. We never play speculatively — worst case
# is a wasted Claude call.

_SPEC_TTL = 60.0            # seconds to keep a cache entry
_SPEC_PREFIX_RATIO = 0.85  # accept a cached partial that covers ≥85% of final
_spec_lock = threading.Lock()
_spec_cache = {}           # norm -> {"reply": str|None, "event": Event, "ts": float}


def _norm(text):
    return re.sub(r"[^a-z0-9 ]", "", text.lower()).strip()


def speculate_async(transcript):
    """Kick off a background Claude call for a partial transcript (cached).

    Returns a status: 'new' | 'cached' | 'skipped-budget' | 'skipped-tools' | 'empty'."""
    if TOOLS_ENABLED:
        # A speculative reply is generated without running tools, so it could be
        # wrong for a tool-requiring query. Skip reply-prefetch (STT still primed).
        return "skipped-tools"
    key = _norm(transcript)
    if not key:
        return "empty"
    with _spec_lock:
        now = time.time()
        for k in [k for k, e in _spec_cache.items() if now - e["ts"] > _SPEC_TTL]:
            del _spec_cache[k]
        if key in _spec_cache:
            return "cached"
        if not _spec_budget_ok():
            return "skipped-budget"  # preserve rate-limit budget for the real turn
        entry = {"reply": None, "event": threading.Event(), "ts": now}
        _spec_cache[key] = entry

    def _run():
        try:
            reply = think(transcript)
        except Exception:  # noqa: BLE001
            reply = None
        with _spec_lock:
            entry["reply"] = reply
        entry["event"].set()

    threading.Thread(target=_run, daemon=True).start()
    return "new"


def lookup_speculation(final_transcript, wait_s=1.5):
    """Return a cached speculative reply matching the final transcript, or None."""
    key = _norm(final_transcript)
    if not key:
        return None
    with _spec_lock:
        best = None
        for k, e in _spec_cache.items():
            match = k == key or (
                key.startswith(k) and len(k) >= _SPEC_PREFIX_RATIO * len(key)
            )
            if match and (best is None or len(k) > len(best[0])):
                best = (k, e)
    if best is None:
        return None
    entry = best[1]
    if entry["reply"] is None:
        entry["event"].wait(wait_s)  # in flight — wait briefly
    return entry["reply"]


def cast(wav_path):
    """Cast a local WAV to the speaker via pychromecast + a tiny HTTP server."""
    import pychromecast
    from pychromecast.controllers.media import MediaController
    import http.server
    import socketserver
    import threading

    # Serve the file from its own directory on an ephemeral port.
    directory = os.path.dirname(wav_path)
    filename = os.path.basename(wav_path)

    handler = lambda *a, **k: http.server.SimpleHTTPRequestHandler(  # noqa: E731
        *a, directory=directory, **k
    )
    httpd = socketserver.TCPServer(("0.0.0.0", 0), handler)
    serve_port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    try:
        chromecasts, browser = pychromecast.get_listed_chromecasts(
            friendly_names=[CAST_DEVICE]
        )
        if not chromecasts:
            raise RuntimeError(f"Cast device {CAST_DEVICE!r} not found")
        cc = chromecasts[0]
        cc.wait()
        cc.set_volume(CAST_VOLUME)

        my_ip = cc.socket_client.socket.getsockname()[0]
        url = f"http://{my_ip}:{serve_port}/{filename}"
        print(f"[cast] device={CAST_DEVICE!r} serving {url}", flush=True)

        mc = cc.media_controller
        mc.play_media(url, "audio/wav")
        mc.block_until_active()
        import time

        # Phase 1: wait for the device to actually START playing (it has to
        # fetch the file first — status is briefly IDLE right after play_media).
        start_deadline = time.time() + 15
        while time.time() < start_deadline:
            mc.update_status()
            if mc.status.player_state in ("PLAYING", "BUFFERING"):
                break
            time.sleep(0.3)
        # Phase 2: keep the HTTP server alive until playback finishes.
        play_deadline = time.time() + 30
        while time.time() < play_deadline:
            mc.update_status()
            if mc.status.player_state not in ("PLAYING", "BUFFERING"):
                break
            time.sleep(0.3)
        pychromecast.discovery.stop_discovery(browser)
    finally:
        httpd.shutdown()


def speak(text):
    fd, wav_path = tempfile.mkstemp(prefix="wes_reply_", suffix=".wav")
    os.close(fd)
    try:
        synthesize(text, wav_path)
        cast(wav_path)
    finally:
        try:
            os.remove(wav_path)
        except OSError:
            pass


# --- HTTP API ---------------------------------------------------------------


@app.route("/health")
def health():
    return jsonify(
        ok=True,
        llm=(
            f"local ({LOCAL_LLM_MODEL})"
            + (f" + {ESCALATE_MODEL} escalation (thinking)"
               if ESCALATE and ESCALATE_MODEL
               else " + claude escalation"
               if ESCALATE and os.environ.get("ANTHROPIC_API_KEY") else "")
            if LLM_BACKEND == "local"
            else "claude" if os.environ.get("ANTHROPIC_API_KEY")
            else "echo (no API key)"
        ),
        output=OUTPUT_MODE,
        cast_device=CAST_DEVICE if OUTPUT_MODE == "cast" else None,
    )


@app.route("/usage")
def usage_route():
    """Token usage rollup by model + call source, with a USD estimate of what
    the local tokens would have cost on Claude Haiku ("saved") and what the
    Claude calls actually cost. ?days=7 limits the window."""
    return jsonify(usage_summary(request.args.get("days", type=float)))


@app.route("/turns")
def turns_route():
    """Last n exchanges, newest first: what was asked, the reply, tools run,
    escalated y/n. ?n=20 sets the count, ?channel=voice filters. Feeds the
    Grafana "Recent turns" table; also handy from curl or the Discord bot."""
    n = max(1, min(request.args.get("n", default=20, type=int), TURNS_MAX))
    return jsonify(turns=recent_turns(n, request.args.get("channel")))


@app.route("/metrics")
def metrics_route():
    """Prometheus exposition (wes_llm_* counters + python runtime defaults).
    Scraped by the Pi's Prometheus every 15s — docs/observability.md."""
    return Response(generate_latest(), mimetype=CONTENT_TYPE_LATEST)


@app.route("/respond_text", methods=["POST"])
def respond_text():
    """Body: JSON {"text": str, "channel"?: str}. Returns {reply, timing}.
    Text-in/text-out — no STT, no TTS, no speaker. The entry point for remote
    text frontends (the Discord bot); each frontend names its own channel so
    its conversation memory stays separate from the in-house voice one."""
    data = request.get_json(silent=True) or {}
    text = (data.get("text") or "").strip()
    channel = (data.get("channel") or "text").strip() or "text"
    if not text:
        return jsonify(error='empty text; POST JSON {"text": ...}'), 400

    print(f"[respond_text] ({channel}) {text!r}", flush=True)
    t0 = time.perf_counter()
    reply = think(text, channel=channel)
    llm_ms = round((time.perf_counter() - t0) * 1000)
    print(f"[respond_text] reply: {reply!r} ({llm_ms}ms)", flush=True)
    record_turn(text, reply, channel=channel)
    return jsonify(reply=reply, timing={"llm_ms": llm_ms})


@app.route("/announce", methods=["POST"])
def announce_route():
    """Body: JSON {"event": str, "channel"?: str}. Jarvis proactively phrases
    an internal system event (a monitoring alert today; scheduled actions
    later) as a natural message to the owner, records it in that channel's
    memory, and returns {reply}. The caller (the Discord bot) delivers it and
    supplies the event context — see docs/observability.md."""
    data = request.get_json(silent=True) or {}
    event = (data.get("event") or "").strip()
    channel = (data.get("channel") or "discord").strip() or "discord"
    if not event:
        return jsonify(error='empty event; POST JSON {"event": ...}'), 400
    print(f"[announce] ({channel}) {event!r}", flush=True)
    reply = announce(event, channel=channel)
    print(f"[announce] reply: {reply!r}", flush=True)
    return jsonify(reply=reply)


@app.route("/respond", methods=["POST"])
def respond():
    """Body: raw WAV bytes (16kHz mono int16). Returns {transcript, reply}."""
    wav_bytes = request.get_data()
    if not wav_bytes:
        return jsonify(error="empty body; POST WAV bytes"), 400

    # Log incoming audio duration so we can spot a silent/empty mic capture.
    try:
        with wave.open(io.BytesIO(wav_bytes)) as w:
            dur = w.getnframes() / float(w.getframerate() or 1)
        print(f"[respond] received {len(wav_bytes)} bytes, {dur:.1f}s audio", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"[respond] received {len(wav_bytes)} bytes (wav header unreadable: {e})", flush=True)

    t0 = time.perf_counter()
    transcript = transcribe(wav_bytes)
    t_stt = time.perf_counter()
    print(f"[respond] transcript: {transcript!r}", flush=True)
    reply = think(transcript)
    t_llm = time.perf_counter()
    print(f"[respond] reply: {reply!r}", flush=True)
    record_turn(transcript, reply)

    stt_ms = round((t_stt - t0) * 1000)
    llm_ms = round((t_llm - t_stt) * 1000)

    if OUTPUT_MODE == "return":
        # Synthesize and hand the audio back to the Pi to play locally.
        from urllib.parse import quote

        fd, wav_path = tempfile.mkstemp(prefix="wes_reply_", suffix=".wav")
        os.close(fd)
        try:
            synthesize(reply, wav_path)
            audio = open(wav_path, "rb").read()
        finally:
            try:
                os.remove(wav_path)
            except OSError:
                pass
        tts_ms = round((time.perf_counter() - t_llm) * 1000)
        print(
            f"[timing] stt={stt_ms}ms llm={llm_ms}ms tts={tts_ms}ms "
            f"(server total {stt_ms + llm_ms + tts_ms}ms), audio={len(audio)}B",
            flush=True,
        )
        return app.response_class(
            audio,
            mimetype="audio/wav",
            headers={
                "X-Transcript": quote(transcript),
                "X-Reply": quote(reply),
                "X-Stt-Ms": str(stt_ms),
                "X-Llm-Ms": str(llm_ms),
                "X-Tts-Ms": str(tts_ms),
            },
        )

    # OUTPUT_MODE == "cast"
    try:
        speak(reply)
        cast_ms = round((time.perf_counter() - t_llm) * 1000)
        print(
            f"[timing] stt={stt_ms}ms llm={llm_ms}ms cast(tts+play)={cast_ms}ms",
            flush=True,
        )
    except Exception as e:  # noqa: BLE001
        print(f"[respond] cast FAILED: {e!r}", flush=True)
        cast_ms = None
    return jsonify(
        transcript=transcript, reply=reply,
        timing={"stt_ms": stt_ms, "llm_ms": llm_ms, "cast_ms": cast_ms},
    )


@app.route("/respond_stream", methods=["POST"])
def respond_stream():
    """Body: WAV bytes. Streams raw s16le PCM (22050Hz mono) of the reply back as
    Claude generates it — sentence-by-sentence TTS, so the first audio starts
    before the full reply exists. X-Transcript / X-Stt-Ms in headers."""
    wav_bytes = request.get_data()
    if not wav_bytes:
        return jsonify(error="empty body; POST WAV bytes"), 400

    from flask import stream_with_context
    from urllib.parse import quote

    t0 = time.perf_counter()
    transcript = transcribe(wav_bytes)
    t_stt = time.perf_counter()
    stt_ms = round((t_stt - t0) * 1000)
    cached = None if TOOLS_ENABLED else lookup_speculation(transcript)
    spec = "HIT" if cached is not None else ("tools" if TOOLS_ENABLED else "miss")
    print(f"[stream] transcript: {transcript!r} (stt {stt_ms}ms, spec {spec})", flush=True)
    voice = get_voice()
    # Cache hit: skip Claude, synthesize the already-generated reply. Miss: stream
    # the reply from Claude token-by-token.
    source = iter([cached]) if cached is not None else stream_reply(transcript)

    def generate():
        buf = ""
        full = []
        t_first = None
        completed = False
        try:
            for piece in source:
                buf += piece
                full.append(piece)
                while True:
                    sent, rest = next_sentence(buf)
                    if sent is None:
                        break
                    buf = rest
                    s = tts_clean(sent)
                    if not s:
                        continue
                    for ac in voice.synthesize(s):
                        if t_first is None:
                            t_first = time.perf_counter()
                        yield ac.audio_int16_bytes
            if tts_clean(buf):
                for ac in voice.synthesize(tts_clean(buf)):
                    if t_first is None:
                        t_first = time.perf_counter()
                    yield ac.audio_int16_bytes
            completed = True
        finally:
            # A client abort (barge-in) lands here with completed=False: the
            # partial reply is what the user heard — record it, tagged.
            record_spoken_turn(transcript, "".join(full), completed)
            reply = "".join(full)
            ttfa = round((t_first - t_stt) * 1000) if t_first else None
            total = round((time.perf_counter() - t0) * 1000)
            print(f"[stream] reply: {reply!r}", flush=True)
            print(
                f"[timing-stream] stt={stt_ms}ms spec={spec} "
                f"ttfa_after_stt={ttfa}ms total={total}ms",
                flush=True,
            )

    resp = app.response_class(
        stream_with_context(generate()), mimetype="application/octet-stream"
    )
    resp.headers["X-Transcript"] = quote(transcript)
    resp.headers["X-Stt-Ms"] = str(stt_ms)
    resp.headers["X-Spec"] = spec
    resp.headers["X-Sample-Rate"] = "22050"
    return resp


@app.route("/reset_conversation", methods=["POST"])
def reset_conversation_route():
    """Clear conversation memory — one channel via JSON {"channel": ...}, or
    every channel when the body is empty. Used by the eval harness so golden
    cases stay independent, and available for an explicit 'new conversation'."""
    data = request.get_json(silent=True) or {}
    return jsonify(cleared_turns=reset_conversation(data.get("channel")))


@app.route("/prefetch_scene", methods=["POST"])
def prefetch_scene_ep():
    """Body: JPEG captured by the Pi at wake-word time. Describe it in the
    background so a later describe_scene tool call is instant."""
    jpeg = request.get_data()
    identities = None
    hdr = request.headers.get("X-Identities")
    if hdr:
        try:
            identities = json.loads(urllib.parse.unquote(hdr))
        except Exception:  # noqa: BLE001
            identities = None
    if jpeg:
        threading.Thread(
            target=prefetch_scene, args=(jpeg, identities), daemon=True
        ).start()
    return jsonify(ok=True)


@app.route("/speculate", methods=["POST"])
def speculate():
    """Body: partial WAV (audio-so-far). Transcribes it and kicks off a
    speculative Claude call cached by transcript. Fire-and-forget from the Pi."""
    wav_bytes = request.get_data()
    if not wav_bytes:
        return jsonify(error="empty body"), 400
    t0 = time.perf_counter()
    transcript = transcribe(wav_bytes)
    stt_ms = round((time.perf_counter() - t0) * 1000)
    status = speculate_async(transcript) if transcript else "empty"
    print(
        f"[speculate] partial ({stt_ms}ms): {transcript!r} "
        f"[{status}, {_llm_calls_last_min()} calls/min]",
        flush=True,
    )
    return jsonify(transcript=transcript)


def warmup():
    """Load + tiny-inference the STT and TTS models so the first real request
    doesn't pay the model-load / first-inference cost."""
    import numpy as np

    t = time.perf_counter()
    print("[warmup] loading models...", flush=True)
    try:
        w = get_whisper()
        # 1s of silence forces the decode graph to initialize.
        list(w.transcribe(np.zeros(16000, dtype=np.float32), language="en")[0])
        print("[warmup] STT ready", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"[warmup] STT warmup skipped: {e!r}", flush=True)
    try:
        for _ in get_voice().synthesize("Ready."):
            pass
        print("[warmup] TTS ready", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"[warmup] TTS warmup skipped: {e!r}", flush=True)
    # Load the Ollama model(s) into VRAM (keep_alive -1) so the first real
    # request isn't cold. With WES_LLM=local and VLM_MODEL == LOCAL_LLM_MODEL
    # this is one model serving both vision and chat.
    warm = set()
    if TOOLS_ENABLED:
        warm.add(VLM_MODEL)
    if LLM_BACKEND == "local":
        warm.add(LOCAL_LLM_MODEL)
    for model in warm:
        try:
            payload = json.dumps({
                "model": model, "prompt": "hi", "stream": False, "keep_alive": -1,
            }).encode()
            req = urllib.request.Request(
                f"{OLLAMA_URL}/api/generate", data=payload,
                headers={"Content-Type": "application/json"},
            )
            urllib.request.urlopen(req, timeout=180).read()
            print(f"[warmup] {model} ready (resident)", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"[warmup] {model} warmup skipped: {e!r}", flush=True)
    print(f"[warmup] done in {round((time.perf_counter() - t) * 1000)}ms", flush=True)


if __name__ == "__main__":
    # Scheduled-task stdout is cp1252; printing a transcript/reply containing
    # emoji (Discord turns can) would raise UnicodeEncodeError mid-request.
    for _s in (sys.stdout, sys.stderr):
        if hasattr(_s, "reconfigure"):
            _s.reconfigure(encoding="utf-8", errors="replace")
    warmup()
    app.run(host=HOST, port=PORT, threaded=True)
