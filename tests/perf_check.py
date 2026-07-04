"""Performance regression check for the WES pipeline (production streaming path).

Runs a fixed synthesized WAV through /respond_stream a few times, records the
median stt/ttfa/total latency to perf_history_stream.csv, and flags a regression
if a metric is worse than median(recent runs) * FACTOR + SLACK_MS. Exit code 1 on
regression.

    cd Z:\\wes\\tests && python perf_check.py [runs]   (default 3, from the wes-pc venv)

ttfa_ms (request start -> first reply audio byte) is the metric users feel —
it's the perceived latency the streaming design exists to minimize.

Run after meaningful changes to track latency over time and catch slowdowns.
(perf_history.csv is the retired history of the old blocking /respond path.)
"""
import csv
import os
import statistics
import subprocess
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(REPO, "pc"))
sys.path.insert(0, HERE)

from stream_client import post_stream  # noqa: E402

URL = os.environ.get("WES_TEST_URL", "http://127.0.0.1:8080")
HISTORY = os.path.join(HERE, "perf_history_stream.csv")
METRICS = ("stt_ms", "ttfa_ms", "total_ms")
FIELDS = ["ts", "git"] + list(METRICS) + ["spec", "audio_s", "transcript"]
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
    res = post_stream(URL, wav)
    if res["ttfa_ms"] is None:  # empty reply stream — count it as the full time
        res["ttfa_ms"] = res["total_ms"]
    return res


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
    import urllib.request
    try:
        urllib.request.urlopen(URL + "/health", timeout=5).read()
    except Exception as e:  # noqa: BLE001
        print(f"server not reachable at {URL}: {e}")
        sys.exit(2)

    wav = make_speech_wav()
    one_run(wav)  # warm, discarded
    runs = [one_run(wav) for _ in range(runs_n)]
    med = {k: round(statistics.median(r[k] for r in runs)) for k in METRICS}
    last = runs[-1]

    print(f"=== this run (median of {runs_n}, /respond_stream) ===")
    for k in METRICS:
        print(f"  {k:9s} {med[k]:6d} ms")
    print(f"  spec={last['spec']} audio_s={last['audio_s']}")
    print(f"  transcript: {last['transcript']!r}")

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
            "git": git_rev(), **med,
            "spec": last["spec"], "audio_s": last["audio_s"],
            "transcript": last["transcript"],
        })

    print("REGRESSION DETECTED" if status else "OK (within thresholds)")
    sys.exit(status)


if __name__ == "__main__":
    main()
