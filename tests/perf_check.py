"""Performance regression check for the WES turn path.

Runs a fixed question through /respond_text a few times, records the median
latency to perf_history_text.csv, and flags a regression if a metric is worse
than median(recent runs) * FACTOR + SLACK_MS. Exit code 1 on regression.

    python tests/perf_check.py [runs]      (default 3, from the wes-pc venv)

WHAT IS MEASURED CHANGED ON 2026-09-02. The metric used to be **ttfa_ms** —
request start to the first byte of reply *audio* — because the streaming design
existed to get a voice talking before the reply was finished, and that is the
number a person standing in the room actually felt. The Pi that played that
audio was repurposed (archive/pi/README.md), so there is no first audio byte and
no partial delivery: a text turn is delivered whole, and the only latency left
is how long the whole turn takes.

  total_ms — request start -> reply in hand (the whole turn, HTTP included)
  llm_ms   — the server's own view of the model call, from its timing block

The old history is preserved at perf_history_stream.csv, which this no longer
appends to: its stt/ttfa/total columns measured a different pipeline, and
continuing the series would compare a spoken first syllable against a finished
paragraph. (perf_history.csv is older still — the retired blocking /respond.)

Run after meaningful changes to track latency over time and catch slowdowns.
"""
import csv
import json
import os
import statistics
import subprocess
import sys
import time
import urllib.request
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(REPO, "pc"))
sys.path.insert(0, HERE)

URL = os.environ.get("WES_TEST_URL", "http://127.0.0.1:8080")
HISTORY = os.path.join(HERE, "perf_history_text.csv")
METRICS = ("llm_ms", "total_ms")
FIELDS = ["ts", "git"] + list(METRICS) + ["reply_chars", "question"]
FACTOR = 1.5        # allow 50% drift over baseline...
SLACK_MS = 500      # ...plus this absolute slack, before flagging a regression

# The same question the streaming check used, so the two histories are at least
# measuring the same work: it routes through the get_datetime tool, making this
# a full router -> tool -> answer round trip rather than bare generation.
QUESTION = "What time is it right now?"
# Its own channel, so a perf run never disturbs the Discord conversation window.
CHANNEL = "perf"


def one_run():
    req = urllib.request.Request(
        URL + "/respond_text",
        data=json.dumps({"text": QUESTION, "channel": CHANNEL}).encode(),
        # A test harness must not be able to mutate the owner's real fantasy
        # account, whatever the model decides to call. See tests/eval_turns.py.
        headers={"Content-Type": "application/json", "X-WES-No-Writes": "1"},
        method="POST",
    )
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=180) as r:
        body = json.loads(r.read())
    total_ms = round((time.perf_counter() - t0) * 1000)
    reply = (body.get("reply") or "").strip()
    return {
        "llm_ms": int(body.get("timing", {}).get("llm_ms") or total_ms),
        "total_ms": total_ms,
        "reply_chars": len(reply),
        "reply": reply,
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

    one_run()  # warm, discarded
    runs = [one_run() for _ in range(runs_n)]
    med = {k: round(statistics.median(r[k] for r in runs)) for k in METRICS}
    last = runs[-1]

    print(f"=== this run (median of {runs_n}, /respond_text) ===")
    for k in METRICS:
        print(f"  {k:9s} {med[k]:6d} ms")
    print(f"  question: {QUESTION!r}")
    print(f"  reply:    {last['reply']!r}")

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
            "reply_chars": last["reply_chars"], "question": QUESTION,
        })

    print("REGRESSION DETECTED" if status else "OK (within thresholds)")
    sys.exit(status)


if __name__ == "__main__":
    main()
