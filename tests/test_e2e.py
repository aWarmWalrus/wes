"""End-to-end tests against a running server (skipped unless --run-e2e).

    cd Z:\\wes\\tests && python -m pytest --run-e2e

Needs the WES server running (WES_TEST_URL, default http://127.0.0.1:8080) and a
valid ANTHROPIC_API_KEY on that server. These exercise the real STT -> Claude ->
TTS pipeline, so they cost a Claude call each.
"""
import time
import urllib.parse
import urllib.request

import pytest

pytestmark = pytest.mark.e2e


def _post(url, data, timeout=120):
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "audio/wav"}, method="POST",
    )
    return urllib.request.urlopen(req, timeout=timeout)


def test_health_ok(server_url, server_up):
    if not server_up:
        pytest.skip(f"server not reachable at {server_url}")
    import json
    with urllib.request.urlopen(server_url + "/health", timeout=5) as r:
        body = json.loads(r.read())
    assert body.get("ok") is True


def test_respond_transcribes_and_replies(server_url, server_up, speech_wav):
    if not server_up:
        pytest.skip(f"server not reachable at {server_url}")
    t0 = time.perf_counter()
    r = _post(server_url + "/respond", speech_wav["wav"])
    audio = r.read()
    total_ms = (time.perf_counter() - t0) * 1000

    transcript = urllib.parse.unquote(r.headers.get("X-Transcript", "")).lower()
    reply = urllib.parse.unquote(r.headers.get("X-Reply", ""))

    # STT should recover the key word from the synthesized "What time is it right now".
    assert "time" in transcript, f"transcript was {transcript!r}"
    # We got a spoken reply back as audio.
    assert reply and len(audio) > 1000
    # Warm-pipeline sanity bound (not a tight perf gate — that's perf_check.py).
    assert total_ms < 25000, f"end-to-end took {total_ms:.0f}ms"


def test_datetime_tool_path(server_url, server_up, speech_wav):
    """'What time is it' should route Claude through the get_datetime tool and
    still return a coherent spoken reply (exercises the tool loop end to end)."""
    if not server_up:
        pytest.skip(f"server not reachable at {server_url}")
    r = _post(server_url + "/respond", speech_wav["wav"])
    r.read()
    reply = urllib.parse.unquote(r.headers.get("X-Reply", ""))
    assert reply.strip(), "expected a non-empty reply"
