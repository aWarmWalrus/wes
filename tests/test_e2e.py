"""End-to-end tests against a running server (skipped unless --run-e2e).
(memory test synthesizes two extra piper fixtures on first run)

    cd Z:\\wes\\tests && python -m pytest --run-e2e

Needs the WES server running (WES_TEST_URL, default http://127.0.0.1:8080).
The primary tests hit /respond_stream — the endpoint the Pi actually uses in
production — and exercise the real STT -> LLM (tool loop) -> streaming TTS
pipeline, so each costs an LLM call. One test keeps the legacy blocking
/respond fallback honest.
"""
import urllib.parse
import urllib.request

import pytest

from stream_client import post_stream

pytestmark = pytest.mark.e2e


def test_health_ok(server_url, server_up):
    if not server_up:
        pytest.skip(f"server not reachable at {server_url}")
    import json
    with urllib.request.urlopen(server_url + "/health", timeout=5) as r:
        body = json.loads(r.read())
    assert body.get("ok") is True


def test_stream_transcribes_and_replies(server_url, server_up, speech_wav):
    """The production path: /respond_stream returns the transcript in headers
    and streams non-trivial reply PCM. 'What time is it right now' also routes
    through the get_datetime tool, so this exercises the streaming tool loop."""
    if not server_up:
        pytest.skip(f"server not reachable at {server_url}")
    res = post_stream(server_url, speech_wav["wav"])

    # STT should recover the key word from the synthesized sentence.
    assert "time" in res["transcript"].lower(), f"transcript was {res['transcript']!r}"
    # We got a real spoken reply back as streamed PCM (>0.3s of audio).
    assert res["audio_s"] > 0.3, f"only {res['audio_s']}s of reply audio"
    # The whole point of streaming: first audio well before the full reply.
    assert res["ttfa_ms"] is not None and res["ttfa_ms"] <= res["total_ms"]
    # Warm-pipeline sanity bound (not a tight perf gate — that's perf_check.py).
    assert res["total_ms"] < 25000, f"end-to-end took {res['total_ms']}ms"


def test_stream_headers_present(server_url, server_up, speech_wav):
    """Timing/spec headers the Pi's timing.csv depends on must keep coming."""
    if not server_up:
        pytest.skip(f"server not reachable at {server_url}")
    res = post_stream(server_url, speech_wav["wav"])
    assert res["stt_ms"] > 0, "X-Stt-Ms missing or zero"
    assert res["spec"] in ("HIT", "miss", "tools"), f"unexpected X-Spec {res['spec']!r}"


def _synth(text, name):
    """Piper -> WAV bytes, cached like conftest's speech_wav."""
    import os
    import subprocess
    import tempfile

    import wes_server as ws

    path = os.path.join(tempfile.gettempdir(), name)
    if not os.path.exists(path):
        subprocess.run(
            [ws.PIPER_BIN, "-m", ws.VOICE_MODEL, "-f", path],
            input=text.encode(), check=True, capture_output=True,
        )
    with open(path, "rb") as f:
        return f.read()


def test_conversation_memory_across_turns(server_url, server_up):
    """Two /respond_stream turns form one conversation: a fact stated in turn
    one must be recalled in turn two (server-side sliding-window memory)."""
    if not server_up:
        pytest.skip(f"server not reachable at {server_url}")
    urllib.request.urlopen(
        urllib.request.Request(server_url + "/reset_conversation", data=b""),
        timeout=5).read()
    post_stream(server_url, _synth(
        "Please remember that my favorite color is purple",
        "wes_test_memory1.wav"))
    res = post_stream(server_url, _synth(
        "What is my favorite color", "wes_test_memory2.wav"),
        collect_audio=True)
    # Recall must survive the full loop: reply PCM -> whisper re-transcription.
    reply = _retranscribe_via_headers(res)
    assert "purple" in reply.lower(), f"reply was {reply!r}"


def _retranscribe_via_headers(res):
    """Recover the reply text by re-transcribing the streamed PCM with the
    eval harness's local whisper (same trick eval_turns.py uses)."""
    from eval_turns import retranscribe
    return retranscribe(res["pcm"])


def test_respond_fallback_still_works(server_url, server_up, speech_wav):
    """The legacy blocking /respond (kept as a fallback) still transcribes and
    returns a full WAV reply."""
    if not server_up:
        pytest.skip(f"server not reachable at {server_url}")
    req = urllib.request.Request(
        server_url + "/respond", data=speech_wav["wav"],
        headers={"Content-Type": "audio/wav"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        audio = r.read()
        transcript = urllib.parse.unquote(r.headers.get("X-Transcript", "")).lower()
        reply = urllib.parse.unquote(r.headers.get("X-Reply", ""))
    assert "time" in transcript, f"transcript was {transcript!r}"
    assert reply and len(audio) > 1000
