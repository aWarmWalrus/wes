"""End-to-end tests against a running server (skipped unless --run-e2e).

    python -m pytest tests/ --run-e2e

Needs the WES server running (WES_TEST_URL, default http://127.0.0.1:8080).
These hit `/respond_text` — the endpoint the Discord bot actually uses in
production — and exercise the real router -> tool loop -> reply path, so each
costs an LLM call.

REWRITTEN 2026-09-02. These used to drive `/respond_stream` with piper-synthesized
WAV fixtures and read the reply back by re-transcribing the streamed PCM with a
local Whisper — a genuinely end-to-end check of STT -> LLM -> TTS. The Raspberry
Pi that fed the microphone and played the speaker was repurposed, so both ends of
that pipeline are gone (archive/pi/README.md) and the surviving path is text in,
text out. What is left still covers the parts that carry risk: the tool loop, the
conversation window, and the fact that a real model answers a real question.
"""
import json
import urllib.request

import pytest

pytestmark = pytest.mark.e2e

TIMEOUT = 180  # a tool round plus a possible escalation is not fast


def _post(url, path, body, timeout=TIMEOUT):
    req = urllib.request.Request(
        url + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def _ask(url, text, channel="e2e"):
    """One turn on a channel of this suite's own, so an e2e run never disturbs
    the real Discord conversation window."""
    return _post(url, "/respond_text", {"text": text, "channel": channel})


def test_health_ok(server_url, server_up):
    if not server_up:
        pytest.skip(f"server not reachable at {server_url}")
    with urllib.request.urlopen(server_url + "/health", timeout=5) as r:
        body = json.loads(r.read())
    assert body.get("ok") is True


def test_text_turn_answers_and_times_itself(server_url, server_up):
    """The production path: a question in, a real reply out, with timing.
    'What time is it right now' also routes through the get_datetime tool, so
    this exercises the tool loop rather than just generation."""
    if not server_up:
        pytest.skip(f"server not reachable at {server_url}")
    res = _ask(server_url, "What time is it right now?")

    reply = res.get("reply", "")
    assert reply.strip(), "empty reply"
    assert "ran into an error" not in reply, reply
    assert res["timing"]["llm_ms"] > 0
    # Warm-pipeline sanity bound, not a tight perf gate (that is perf_check.py).
    assert res["timing"]["llm_ms"] < 120000, f"turn took {res['timing']['llm_ms']}ms"


def test_datetime_tool_grounds_the_answer(server_url, server_up):
    """The model must answer from the tool, not from its own idea of the date —
    the whole reason the tool exists. Checked by the year appearing at all."""
    if not server_up:
        pytest.skip(f"server not reachable at {server_url}")
    reply = _ask(server_url, "What is today's date?")["reply"]
    assert any(y in reply for y in ("2025", "2026", "2027", "2028")), reply


def test_conversation_memory_across_turns(server_url, server_up):
    """Two turns form one conversation: a fact stated in turn one must be
    recalled in turn two (server-side sliding-window memory)."""
    if not server_up:
        pytest.skip(f"server not reachable at {server_url}")
    _post(server_url, "/reset_conversation", {"channel": "e2e"}, timeout=10)
    _ask(server_url, "Please remember that my favorite color is purple.")
    reply = _ask(server_url, "What is my favorite color?")["reply"]
    assert "purple" in reply.lower(), f"reply was {reply!r}"


def test_reply_is_not_dressed_for_a_speaker(server_url, server_up):
    """A regression tripwire for the retired voice framing: the prompt used to
    tell the model it was being read aloud, and a leftover of that would show up
    as the model talking about hearing or speaking to a Discord user."""
    if not server_up:
        pytest.skip(f"server not reachable at {server_url}")
    reply = _ask(server_url, "Can you hear me?")["reply"].lower()
    assert "i heard you say" not in reply
