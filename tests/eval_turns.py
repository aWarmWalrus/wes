"""Eval harness, phases 1-2 (docs/eval-design.md): golden set, deterministic
checks, and an LLM judge with selectable backends.

Runs each case in eval/golden.yaml through the LIVE server's /respond_text
(the real router -> tool loop -> reply path) and applies the case's
deterministic checks. Phase 2: each case's `judge:` question is also scored by
one LLM-judge call (correct/concise/natural 0-2 + hallucination flag).

CHANGED 2026-09-02. This used to POST piper-synthesized WAV fixtures to
/respond_stream and read the reply back by re-transcribing the streamed PCM with
a local tiny.en Whisper — which also served as a TTS intelligibility check, and
meant every assertion had to survive a speech round-trip. The Pi that made that
pipeline real was repurposed (archive/pi/README.md), so both ends are gone.
Text in, text out: the deterministic checks got sharper (no more loose regexes
allowing for "Breece Hall" heard as "Breeze Hole"), and the audio-length brevity
bounds became character counts. `transcript_includes` was dropped entirely — it
asserted that STT heard the question, and now we simply send it.

Cases run on the "eval" channel, which the server routes through the deep tier
exactly as it does Discord, so this grades the tier production actually uses.

Judge backends (--judge / WES_EVAL_JUDGE, default haiku):
  haiku   claude-haiku-4-5 — the sharper signal; pennies per run. Use when
          deciding something (prompt/model/routing changes). Needs
          ANTHROPIC_API_KEY; without one the judge turns off with a hint.
  local   gemma4:12b via Ollama — free, key-less, a bit noisier. Use for
          nightly/unattended runs. Deliberately NOT gemma4:e4b: that is the
          model under test, and self-judging is the classic LLM-judge bias.
  both    scores every case with both and prints an agreement summary
          (records the haiku scores). Run occasionally to confirm the local
          judge still tracks haiku before trusting it for nightly gating.
  off     deterministic checks only (--no-judge is an alias).

History rows record which judge scored them, and the judge gate only compares
runs scored by the SAME backend — haiku and local medians are never mixed.

Results append to eval_history.csv; the run FAILS (exit 1) if
  - any case that passed in the previous run fails now (named-case regression,
    printed by id), or
  - the run's judge `correct` average drops more than 0.3 below the median of
    the last 5 recorded runs by the same judge backend.

    C:\\Users\\awarm\\wes-pc\\.venv\\Scripts\\python.exe Z:\\wes\\tests\\eval_turns.py
    ... --only time-basic     # run one case
    ... --url http://...      # non-default server
    ... --judge local         # free local judge (nightly)
    ... --judge both          # judge-agreement check
    ... --no-judge            # deterministic checks only
    ... --web-search          # also run web_search cases (paid API — weekly, not nightly)
"""
import argparse
import csv
import json
import os
import re
import statistics
import sys
import time
import urllib.request

# cp1252 console: a judge note containing '→' etc. must degrade, not crash
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(errors="replace")
    except (AttributeError, ValueError):  # non-reconfigurable (pytest capture)
        pass

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "pc"))

GOLDEN = os.path.join(HERE, "eval", "golden.yaml")
HISTORY = os.path.join(HERE, "eval_history.csv")
SERVER = os.environ.get("WES_TEST_URL", "http://127.0.0.1:8080")
MAX_TOTAL_MS_DEFAULT = 30000

# `transcript` is kept as a column even though it is now just the question we
# sent: the history is read back by case and by run, and dropping a column that
# every recorded row has would make old runs unreadable for no gain.
# spec/stt_ms/ttfa_ms/audio_s went with the audio pipeline; append_history
# migrates the existing file to this header on the next write.
FIELDS = ["ts", "case", "passed", "fails", "transcript", "reply",
          "total_ms", "reply_chars", "judge",
          "judge_correct", "judge_concise", "judge_natural",
          "hallucination", "judge_note"]

JUDGE_BACKEND = os.environ.get("WES_EVAL_JUDGE", "haiku")  # haiku|local|off
JUDGE_MODEL = os.environ.get("WES_EVAL_JUDGE_MODEL", "claude-haiku-4-5")
JUDGE_LOCAL_MODEL = os.environ.get("WES_EVAL_JUDGE_LOCAL_MODEL", "gemma4:12b")
OLLAMA_URL = os.environ.get("WES_OLLAMA_URL", "http://127.0.0.1:11434")
JUDGE_DROP = 0.3          # fail if correct-avg drops more than this vs median
JUDGE_MEDIAN_OF = 5       # ... of the last N recorded runs
# The old version of this told the judge both sides had been round-tripped
# through speech recognition and to read garbled numbers and words charitably.
# Nothing is transcribed any more, so that instruction now only buys the model
# under test undeserved leniency — a garbled number is a real error again.
JUDGE_SYSTEM = (
    "You are grading a home assistant's typed reply. The assistant is Jarvis; "
    "it genuinely has live tools (the date and time, NBA scores and schedules, "
    "the owner's real Yahoo/Sleeper fantasy teams, durable memory), so a reply "
    "citing real data from those is grounded, not a hallucination. Reason in "
    "the note field FIRST, then score; mark incorrect/hallucination only for "
    "clear content errors. Return ONLY a JSON object, no prose."
)


def load_golden():
    import yaml
    with open(GOLDEN, encoding="utf-8") as f:
        cases = yaml.safe_load(f)
    ids = [c["id"] for c in cases]
    assert len(ids) == len(set(ids)), "duplicate case ids in golden.yaml"
    return cases


def case_turns(case):
    """A case is one utterance (`say`) or a multi-turn conversation (`turns`);
    checks always apply to the reply of the LAST turn."""
    return case["turns"] if "turns" in case else [case["say"]]


# Cases run on their own channel so the harness never touches the owner's real
# Discord conversation window, and the server routes it through the deep tier so
# the eval grades the same model Discord gets (WES_DEEP_CHANNELS).
#
# The old value was "voice": /respond_stream named no channel, so it landed on
# the server default, and the reset had to name that same channel.
EVAL_CHANNEL = "eval"


def ask_server(url, text, channel=EVAL_CHANNEL, timeout=180):
    """One turn through /respond_text -> {"reply", "timing": {"llm_ms"}}.

    X-WES-No-Writes: this is a TEST HARNESS, and a test must not mutate the
    owner's real Yahoo account. The eval's "check if my lineup needs changes"
    case makes the model call the executor, which wrote for real at 03:36 every
    eval night until this header existed (2026-08-14). The header suppresses
    fantasy writes for THIS REQUEST ONLY, server-side, so nothing the harness
    provokes can reach a live account.
    """
    req = urllib.request.Request(
        url + "/respond_text",
        data=json.dumps({"text": text, "channel": channel}).encode(),
        headers={"Content-Type": "application/json",
                 "X-WES-No-Writes": "1"},
        method="POST",
    )
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = json.loads(r.read())
    reply = (body.get("reply") or "").strip()
    return {
        "transcript": text,
        "reply": reply,
        "reply_chars": len(reply),
        # The server times the model call; we time the round trip. The wall
        # clock is what the max_total_ms budgets have always meant.
        "total_ms": round((time.perf_counter() - t0) * 1000),
    }


def reset_server_conversation(url, channel=EVAL_CHANNEL):
    """The server keeps conversation memory across turns; clear it so every
    case starts from a blank context and cases stay order-independent.

    **Scoped to ONE channel on purpose.** An empty body means "every channel"
    server-side, and this runs between every case of a 03:30 nightly — so it was
    wiping the owner's Discord history every night. Symptom: WES DMs about a
    real roster move, the owner asks "why did you make that move?" hours later,
    and it has no idea what they mean, because the `[system event]` turn that
    recorded the move was cleared minutes after it landed (2026-08-09).

    A test suite must not have side effects on live user state; the blast radius
    here should be exactly the channel the eval drives."""
    try:
        req = urllib.request.Request(
            url + "/reset_conversation",
            data=json.dumps({"channel": channel}).encode(),
            headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=5).read()
    except Exception as e:  # noqa: BLE001  (older server without the route)
        print(f"! reset_conversation unavailable: {e}")


def check_case(case, res, reply_text):
    """Deterministic checks -> list of failure strings (empty = pass).

    `transcript_includes` was removed on 2026-09-02: it checked that speech
    recognition had heard the question, and the question is now sent as text.
    The brevity bounds moved from seconds of spoken audio to characters."""
    exp = case.get("expect", {})
    fails = []
    rx = exp.get("reply_regex")
    if rx and not re.search(rx, reply_text, re.IGNORECASE):
        fails.append(f"reply_regex {rx!r} unmatched: {reply_text!r}")
    nrx = exp.get("reply_not_regex")
    if nrx and re.search(nrx, reply_text, re.IGNORECASE):
        fails.append(f"reply_not_regex {nrx!r} matched: {reply_text!r}")
    if not reply_text:
        fails.append("empty reply")
    if res["reply_chars"] < exp.get("min_reply_chars", 0):
        fails.append(f"reply too short: {res['reply_chars']} chars")
    if res["reply_chars"] > exp.get("max_reply_chars", 10 ** 6):
        fails.append(f"reply too long: {res['reply_chars']} chars (brevity)")
    if res["total_ms"] > exp.get("max_total_ms", MAX_TOTAL_MS_DEFAULT):
        fails.append(f"too slow: {res['total_ms']}ms")
    return fails


def parse_judge(text):
    """Judge reply text -> scores dict, or None if it isn't valid/complete."""
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        d = json.loads(m.group(0))
        return {
            "judge_correct": int(d["correct"]),
            "judge_concise": int(d["concise"]),
            "judge_natural": int(d["natural"]),
            "hallucination": int(bool(d.get("hallucination", False))),
            "judge_note": str(d.get("note", ""))[:200],
        }
    except (ValueError, KeyError, TypeError):
        return None


def judge_prompt(case, transcript, reply):
    """The one grading prompt — identical for every backend, so backends are
    comparable and --judge both measures the judges, not the prompts."""
    check = case.get(
        "judge", "Is the reply a reasonable answer to the question?")
    now = time.strftime("%A, %B %d %Y, %I:%M %p")
    return (
        f"Actual current date/time (for grading): {now}\n"
        f"Question the user sent: {transcript or '(empty)'}\n"
        f"Reply sent to the user: {reply}\n"
        f"Case-specific check: {check}\n"
        'Return JSON: {"note": "<one line of reasoning>", '
        '"correct": 0-2, "concise": 0-2, "natural": 0-2, '
        '"hallucination": true/false}'
    )


_haiku = None


def _judge_raw_haiku(user_content):
    global _haiku
    if _haiku is None:
        import anthropic
        _haiku = anthropic.Anthropic()
    msg = _haiku.messages.create(
        model=JUDGE_MODEL, max_tokens=250, system=JUDGE_SYSTEM,
        messages=[{"role": "user", "content": user_content}])
    return msg.content[0].text


def _judge_raw_local(user_content):
    # gemma4:12b is already resident on the card as the VLM; a text-only chat
    # call costs no extra VRAM and runs in a couple of seconds. format=json +
    # temperature 0 + think off keep the small judge parseable and repeatable —
    # with thinking left on (the model default) it intermittently mangles the
    # score keys (e.g. "concise_score" for "natural") and the case goes
    # unscored.
    body = json.dumps({
        "model": JUDGE_LOCAL_MODEL, "stream": False, "format": "json",
        "think": False, "options": {"temperature": 0},
        "messages": [{"role": "system", "content": JUDGE_SYSTEM},
                     {"role": "user", "content": user_content}],
    }).encode()
    req = urllib.request.Request(
        OLLAMA_URL + "/api/chat", data=body,
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)["message"]["content"]


def judge_case(case, transcript, reply, backend):
    """One judge call scoring what deterministic checks can't. None on any
    failure — the judge must never break a run."""
    raw = {"haiku": _judge_raw_haiku, "local": _judge_raw_local}[backend]
    try:
        text = raw(judge_prompt(case, transcript, reply))
        scores = parse_judge(text)
        if scores is None:
            # Show the raw reply so a bad judge run is diagnosable from the log.
            print(f"     ! {backend} judge output unparseable — case unscored: "
                  f"{text[:150]!r}")
        return scores
    except Exception as e:  # noqa: BLE001
        print(f"     ! {backend} judge error: {e}")
        return None


def judge_gate(current_avg, prior_avgs):
    """True (= fail the run) if this run's correct-avg dropped too far below
    the median of the recent recorded runs."""
    if current_avg is None or not prior_avgs:
        return False
    baseline = statistics.median(prior_avgs[-JUDGE_MEDIAN_OF:])
    return current_avg < baseline - JUDGE_DROP


def prior_judge_averages(backend):
    """Per-run judge_correct averages from the history, oldest first — only
    runs scored by the SAME backend (different judges have different
    baselines; comparing across them would make the gate meaningless).
    Rows from before the judge column existed were scored by haiku."""
    if not os.path.exists(HISTORY):
        return []
    with open(HISTORY, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    runs = {}  # ts -> [scores]  (dicts keep insertion order = file order)
    for r in rows:
        if r.get("judge_correct") and (r.get("judge") or "haiku") == backend:
            runs.setdefault(r["ts"], []).append(int(r["judge_correct"]))
    return [sum(v) / len(v) for v in runs.values()]


def previous_results():
    """case -> passed? from the most recent run recorded in the history."""
    if not os.path.exists(HISTORY):
        return {}
    with open(HISTORY, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        return {}
    last_ts = rows[-1]["ts"]
    return {r["case"]: r["passed"] == "1" for r in rows if r["ts"] == last_ts}


def append_history(rows):
    if os.path.exists(HISTORY):
        with open(HISTORY, newline="", encoding="utf-8") as f:
            reader = csv.reader(f)
            header = next(reader, None)
        if header is not None and header != FIELDS:
            # schema grew (e.g. phase-2 judge columns): rewrite with the new
            # header, padding old rows with blanks
            with open(HISTORY, newline="", encoding="utf-8") as f:
                old = list(csv.DictReader(f))
            with open(HISTORY, "w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=FIELDS, restval="",
                                   extrasaction="ignore")
                w.writeheader()
                w.writerows(old)
    new = not os.path.exists(HISTORY)
    with open(HISTORY, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if new:
            w.writeheader()
        w.writerows(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", help="run a single case id")
    ap.add_argument("--url", default=SERVER)
    ap.add_argument("--no-history", action="store_true",
                    help="don't append to eval_history.csv (ad-hoc debugging)")
    ap.add_argument("--judge", choices=["haiku", "local", "both", "off"],
                    default=None,
                    help="judge backend (default: WES_EVAL_JUDGE or haiku); "
                         "'both' also scores with local and reports agreement")
    ap.add_argument("--no-judge", action="store_true",
                    help="alias for --judge off (deterministic checks only)")
    ap.add_argument("--web-search", action="store_true",
                    help="include web_search-tagged cases (they hit the PAID "
                         "Anthropic web-search API). Off by default so nightly "
                         "runs stay free; run these weekly. Env: "
                         "WES_EVAL_WEB_SEARCH=1. --only <id> also forces one in.")
    args = ap.parse_args()
    run_web = args.web_search or os.environ.get("WES_EVAL_WEB_SEARCH") == "1"

    backend = args.judge or ("off" if args.no_judge else JUDGE_BACKEND)
    if backend in ("haiku", "both") and \
            not os.environ.get("ANTHROPIC_API_KEY"):
        fallback = "local" if backend == "both" else "off"
        print(f"judge: no ANTHROPIC_API_KEY — {backend} unavailable, using "
              f"{fallback} (hint: --judge local is free and key-less)")
        backend = fallback
    # 'both' grades with both judges but RECORDS haiku (the sharper signal);
    # its purpose is checking that the local judge still tracks haiku
    primary = "haiku" if backend == "both" else backend
    if backend == "off":
        print("judge: OFF")

    cases = load_golden()
    if args.only:
        cases = [c for c in cases if c["id"] == args.only]
        if not cases:
            sys.exit(f"no case with id {args.only!r}")

    prev = previous_results()
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    rows, regressions, agreement, n_pass, n_skip = [], [], [], 0, 0
    for case in cases:
        # A `requires_pi` flag lived here, skipping vision/status cases when the
        # Pi's :8090 didn't answer. The Pi is gone and so are those cases.
        #
        # web_search cases hit the paid API — skip unless explicitly opted in
        # (weekly). --only <id> forces the case in (cases is already filtered).
        if case.get("web_search") and not run_web and not args.only:
            print(f"SKIP {case['id']}  (web-search case; use --web-search / weekly)")
            n_skip += 1
            continue
        reset_server_conversation(args.url)
        for say in case_turns(case):  # checks target the LAST turn
            res = ask_server(args.url, say)
        reply_text = res["reply"]
        fails = check_case(case, res, reply_text)
        ok = not fails
        n_pass += ok
        mark = "PASS" if ok else "FAIL"
        print(f"{mark} {case['id']:22s} "
              f"total={res['total_ms']}ms reply={res['reply_chars']}c")
        for fail in fails:
            print(f"     - {fail}")
        if not ok and prev.get(case["id"]) is True:
            regressions.append(case["id"])
        scores = None
        if primary != "off":
            scores = judge_case(case, res["transcript"], reply_text, primary)
        if scores:
            print(f"     {primary}: correct={scores['judge_correct']} "
                  f"concise={scores['judge_concise']} "
                  f"natural={scores['judge_natural']}"
                  + (" HALLUCINATION" if scores["hallucination"] else "")
                  + f"  ({scores['judge_note']})")
        if backend == "both":
            ls = judge_case(case, res["transcript"], reply_text, "local")
            if ls:
                print(f"     local: correct={ls['judge_correct']} "
                      f"concise={ls['judge_concise']} "
                      f"natural={ls['judge_natural']}"
                      + (" HALLUCINATION" if ls["hallucination"] else "")
                      + f"  ({ls['judge_note']})")
            if scores and ls:
                agreement.append(
                    (case["id"], scores["judge_correct"],
                     ls["judge_correct"]))
        rows.append({
            "ts": ts, "case": case["id"], "passed": int(ok),
            "fails": "; ".join(fails), "transcript": res["transcript"],
            "reply": reply_text, "total_ms": res["total_ms"],
            "reply_chars": res["reply_chars"],
            "judge": primary if scores else "",
            **(scores or {"judge_correct": "", "judge_concise": "",
                          "judge_natural": "", "hallucination": "",
                          "judge_note": ""}),
        })

    scored = [r for r in rows if r["judge_correct"] != ""]
    judge_avg = (sum(r["judge_correct"] for r in scored) / len(scored)
                 if scored else None)
    # read BEFORE appending this run; same-backend runs only
    prior_avgs = prior_judge_averages(primary) if primary != "off" else []

    if rows and not args.no_history and not args.only:
        append_history(rows)
    ran = len(rows)
    print(f"\n{n_pass}/{ran} passed" + (f", {n_skip} skipped" if n_skip else ""))
    if judge_avg is not None:
        print(f"judge ({primary}) correct-avg: {judge_avg:.2f}/2"
              + (f" (recent {primary} median "
                 f"{statistics.median(prior_avgs[-JUDGE_MEDIAN_OF:]):.2f})"
                 if prior_avgs else f" (first {primary}-judged run)"))
    if agreement:
        diffs = [abs(h - l) for _, h, l in agreement]
        off = [f"{c} (haiku {h} vs local {l})"
               for c, h, l in agreement if h != l]
        print(f"judge agreement (correct, {len(agreement)} cases): "
              f"mean |diff| {sum(diffs) / len(diffs):.2f}"
              + ("; disagreed: " + ", ".join(off) if off else "; identical"))
    if regressions:
        print("REGRESSIONS (passed last run, fail now): " + ", ".join(regressions))
        sys.exit(1)
    if judge_gate(judge_avg, prior_avgs):
        print(f"JUDGE REGRESSION: correct-avg {judge_avg:.2f} dropped >"
              f"{JUDGE_DROP} below the recent median")
        sys.exit(1)
    if ran and n_pass < ran and not prev:
        print("(first recorded run — failures above are baseline, not regressions)")


if __name__ == "__main__":
    main()
