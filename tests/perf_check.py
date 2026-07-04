"""Performance regression check for the WES pipeline.

Runs a fixed synthesized WAV through /respond a few times, records the median
stt/llm/tts/total latency to perf_history.csv, and flags a regression if a metric
is worse than median(recent runs) * FACTOR + SLACK_MS. Exit code 1 on regression.

    cd Z:\\wes\\tests && python perf_check.py [runs]   (default 3, from the wes-pc venv)

Run it after meaningful changes to track latency over time and catch slowdowns.
"""
import csv
import os
import statistics
import subprocess
import sys
import urllib.parse
import urllib.request
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(REPO, "pc"))

URL = os.environ.get("WES_TEST_URL", "http://127.0.0.1:8080")
HISTORY = os.path.join(HERE, "perf_history.csv")
METRICS = ("stt_ms", "llm_ms", "tts_ms", "total_ms")
FIELDS = ["ts", "git"] + list(METRICS) + ["transcript"]
FACTOR = 1.5        # allow 50% drift over baseline...
SLACK_MS = 500      # ...plus this absolute slack, before flagging a regression


def make_speech_wav():
    import wes_server as ws

    path = os.path.join(HERE, "fixtures", "speech.wav")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if not os.path.exists(path):
        subprocess.run(
            [ws.PIPER_BIN, "-m", ws.VOICE_MODEL, "-f", path],
            input=b"What time is it right now", check=True, capture_output=True,
        )
    with open(path, "rb") as f:
        return f.read()


def one_run(wav):
    import time
    t0 = time.perf_counter()
    req = urllib.request.Request(
        URL + "/respond", data=wav,
        headers={"Content-Type": "audio/wav"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        h = r.headers
        r.read()
    return {
        "stt_ms": int(h.get("X-Stt-Ms", 0)),
        "llm_ms": int(h.get("X-Llm-Ms", 0)),
        "tts_ms": int(h.get("X-Tts-Ms", 0)),
        "total_ms": round((time.perf_counter() - t0) * 1000),
        "transcript": urllib.parse.unquote(h.get("X-Transcript", "")),
    }


def git_rev():
    try:
        out = subprocess.run(
            ["git", "-C", REPO, "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True,
        ).stdout.strip()
        return out or "n/a"
    except Exception:  # noqa: BLE001
        return "n/a"


def load_history():
    if not os.path.exists(HISTORY):
        return []
    with open(HISTORY, newline="") as f:
        return list(csv.DictReader(f))


def main():
    runs_n = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    try:
        urllib.request.urlopen(URL + "/health", timeout=5).read()
    except Exception as e:  # noqa: BLE001
        print(f"server not reachable at {URL}: {e}")
        sys.exit(2)

    wav = make_speech_wav()
    one_run(wav)  # warm, discarded
    runs = [one_run(wav) for _ in range(runs_n)]
    med = {k: round(statistics.median(r[k] for r in runs)) for k in METRICS}
    transcript = runs[-1]["transcript"]

    print(f"=== this run (median of {runs_n}) ===")
    for k in METRICS:
        print(f"  {k:9s} {med[k]:6d} ms")
    print(f"  transcript: {transcript!r}")

    status = 0
    hist = load_history()
    if hist:
        recent = hist[-10:]
        # round(): median of an even-length history is a float, breaking :d below
        base = {k: round(statistics.median(int(h[k]) for h in recent)) for k in METRICS}
        print(f"=== vs baseline (median of last {len(recent)} recorded) ===")
        for k in METRICS:
            limit = base[k] * FACTOR + SLACK_MS
            flag = "  <-- REGRESSION" if med[k] > limit else ""
            if flag:
                status = 1
            print(f"  {k:9s} now={med[k]:6d}  base={base[k]:6d}  limit={round(limit):6d}{flag}")
    else:
        print("(no history yet — this run establishes the baseline)")

    write_header = not os.path.exists(HISTORY)
    with open(HISTORY, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if write_header:
            w.writeheader()
        w.writerow({
            "ts": datetime.now().isoformat(timespec="seconds"),
            "git": git_rev(), **med, "transcript": transcript,
        })

    print("REGRESSION DETECTED" if status else "OK (within thresholds)")
    sys.exit(status)


if __name__ == "__main__":
    main()
