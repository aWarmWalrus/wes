"""Shared client for the production streaming endpoint (/respond_stream).

Used by test_e2e.py and perf_check.py so both exercise the path the Pi actually
uses, with the same timing definitions the Pi logs to timing.csv:

    headers_ms — request start -> response headers (≈ STT + transfer)
    ttfa_ms    — request start -> first reply PCM byte (time-to-first-audio)
    total_ms   — request start -> last reply PCM byte
"""
import time
import urllib.parse
import urllib.request

SAMPLE_RATE = 22050  # s16le mono PCM, per X-Sample-Rate


def post_stream(server_url, wav_bytes, timeout=120, collect_audio=False):
    """POST WAV bytes to /respond_stream; drain the PCM stream.

    Returns {transcript, spec, stt_ms, headers_ms, ttfa_ms, total_ms,
             audio_bytes, audio_s} — plus the raw reply PCM under "pcm" when
    collect_audio=True (the eval runner re-transcribes it; keep it off for
    perf/e2e so they don't hold whole replies in memory)."""
    t0 = time.perf_counter()
    req = urllib.request.Request(
        server_url + "/respond_stream", data=wav_bytes,
        # X-WES-No-Writes: every caller of this client is a TEST HARNESS (the
        # nightly eval, perf_check, e2e), and a test must not mutate the owner's
        # real Yahoo account. The eval's "check if my lineup needs changes" case
        # made the model call the executor, which wrote for real at 03:36 every
        # eval night until 2026-08-14. The server's LIVE_WRITES switch cannot
        # help here — it lives in the server process, not the harness — so the
        # suppression has to travel WITH the request.
        headers={"Content-Type": "audio/wav", "X-WES-No-Writes": "1"},
        method="POST",
    )
    pcm = bytearray()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        headers_ms = (time.perf_counter() - t0) * 1000
        h = r.headers
        t_first = None
        n_bytes = 0
        while True:
            chunk = r.read(4096)
            if not chunk:
                break
            if t_first is None:
                t_first = time.perf_counter()
            n_bytes += len(chunk)
            if collect_audio:
                pcm.extend(chunk)
    t_done = time.perf_counter()
    res = {
        "transcript": urllib.parse.unquote(h.get("X-Transcript", "")),
        "spec": h.get("X-Spec", "?"),
        "stt_ms": int(h.get("X-Stt-Ms", 0) or 0),
        "headers_ms": round(headers_ms),
        "ttfa_ms": round((t_first - t0) * 1000) if t_first else None,
        "total_ms": round((t_done - t0) * 1000),
        "audio_bytes": n_bytes,
        "audio_s": round(n_bytes / 2 / SAMPLE_RATE, 2),
    }
    if collect_audio:
        res["pcm"] = bytes(pcm)
    return res
