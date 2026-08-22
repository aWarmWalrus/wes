r"""Golden scenarios for the draft agent — does it get the KNOWN cases right?

    & C:\Users\awarm\wes-pc\.venv\Scripts\python.exe C:\Users\awarm\wes\tests\draft_eval.py
    ... --with-claude          also run the same cases against Claude
    ... --only qb-over-rb      one case
    ... --repeat 3             each case N times, to expose flakiness

WHY THIS EXISTS
Every model change so far has been judged on "how often does it agree with the
engine's top pick", measured on a replayed draft. That proxy is bad in a
specific way: it has NO notion of a right answer. It cannot tell "the model
stopped thinking" from "the model was right to agree", and on a 15-pick replay
with no ground truth it moves by two picks on noise. It sent me the wrong way
at least once.

These are hand-built boards where the correct answer is known by construction:
a quarterback priced above a running back when the RB slot is the empty one, a
kicker with a tempting number in round 9, two backs separated only by their
career arc. The board is synthetic on purpose — real boards are confounded, and
a case that tests one thing has to hold everything else equal.

HARD vs SOFT, and why they are reported apart
A `must` case has one defensible answer and failing it is a bug: taking a
kicker in round 9 is wrong, not a matter of taste. A `prefer` case has a better
and a worse answer but reasonable drafters differ. Scoring them together would
let soft wins paper over hard failures, so they are counted separately and the
exit code follows the HARD score alone.

What this still cannot tell you: whether the resulting team scores more points.
Nothing short of a season can. It tells you the agent is not making mistakes we
have already identified, which is the thing regressions are made of.
"""
import argparse
import json
import os
import sys
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "pc"))

import wes_draft_agent as agent  # noqa: E402

OLLAMA_URL = os.environ.get("WES_OLLAMA_URL", "http://127.0.0.1:11434")
CLAUDE_MODEL = os.environ.get("WES_REPLAY_CLAUDE", "claude-haiku-4-5-20251001")


def _p(key, name, pos, vor, need=0.0, **extra):
    """One shortlist candidate. Defaults chosen so a case only has to state
    the thing it is testing."""
    row = {"player_key": key, "name": name, "positions": [pos], "team": "XXX",
           "vor": vor, "need_bump": need, "fit_penalty": 0.0,
           "fit_reasons": [], "adj_value": round(vor + need, 2),
           "bye": 7, "market_rank": 50, "injury": None}
    row.update(extra)
    return row


def _ctx(rnd, unfilled, roster, **extra):
    ctx = {"round": rnd, "pick_number": rnd * 12, "picks_until_next_turn": 0,
           "starting_slots": ["QB", "RB", "RB", "WR", "WR", "TE", "W/R/T",
                              "W/R/T", "K", "DEF"] + ["BN"] * 5,
           "roster_so_far": roster, "still_unfilled": unfilled,
           "bye_counts": {}, "phase": "starters" if unfilled else "depth",
           "recent_picks_by_position": {}}
    ctx.update(extra)
    return ctx


def _roster(*positions):
    return [{"name": f"Have{i}", "position": p, "team": "AAA", "bye": 5}
            for i, p in enumerate(positions)]


# --- the scenarios ---------------------------------------------------------
# `want` is the player_key that must be chosen; `not_want` is a set that must
# NOT be. `kind` is "must" (a bug if failed) or "prefer" (taste).
CASES = [
    {
        "id": "qb-over-rb",
        "kind": "must",
        "why": "The RB slot is empty and the QB slot is filled. A higher VOR "
               "at a position we cannot start is worth nothing.",
        "candidates": [_p("qb", "Big Arm", "QB", 8.0),
                       _p("rb", "Empty Slot Back", "RB", 6.0, need=2.0)],
        "context": _ctx(4, {"RB": 1}, _roster("QB", "WR", "WR", "TE")),
        "want": "rb",
    },
    {
        "id": "no-early-kicker",
        "kind": "must",
        "why": "Round 9 with skill slots open. A kicker's VOR is not "
               "comparable and replacement level there is flat -- this is the "
               "real failure from 2026-08-21, where it took Fairbairn in "
               "round 10 over Garrett Wilson.",
        "candidates": [_p("k", "Tempting Boot", "K", 9.0, need=0.25),
                       _p("wr", "Real Player", "WR", 5.0, need=0.5),
                       _p("rb", "Other Real Player", "RB", 4.6, need=0.5)],
        "context": _ctx(9, {"K": 1, "DEF": 1},
                        _roster("QB", "RB", "RB", "WR", "WR", "TE", "RB")),
        "not_want": {"k"},
    },
    {
        "id": "late-kicker-is-fine",
        "kind": "must",
        "why": "The mirror of the above: in round 14 with K still empty and "
               "only bench bodies opposite, the kicker IS the pick. A rule "
               "that never takes one is as broken as one that takes it early.",
        "candidates": [_p("k", "Last Boot", "K", 3.0, need=0.25),
                       _p("wr5", "Fifth Receiver", "WR", 1.2, need=-2.0)],
        "context": _ctx(14, {"K": 1}, _roster(*(["RB"] * 4 + ["WR"] * 4
                                                + ["QB", "TE", "DEF"]))),
        "want": "k",
    },
    {
        "id": "trend-breaks-a-tie",
        "kind": "prefer",
        "why": "Two backs, same VOR, same need. The only thing separating "
               "them is the career arc, which is what the notes are for.",
        "candidates": [
            _p("rising", "Ascending Back", "RB", 5.0, need=1.0,
               notes={"trajectory": "24, 3rd year — 6.1 -> 12.4 pts/g, "
                                    "trending up",
                      "role": "RB1 — starter"}),
            _p("falling", "Fading Back", "RB", 5.0, need=1.0,
               notes={"trajectory": "31, 10th year — 14.0 -> 8.2 pts/g, "
                                    "trending down",
                      "role": "RB2 behind Someone Else"})],
        "context": _ctx(6, {"RB": 1}, _roster("QB", "WR", "WR", "TE")),
        "want": "rising",
    },
    {
        "id": "injury-breaks-a-tie",
        "kind": "prefer",
        "why": "Same VOR, one of them cannot practise. Questionable is a "
               "judgment call we deliberately leave to the model rather than "
               "excluding -- so it has to actually make it.",
        "candidates": [
            _p("fit", "Healthy Back", "RB", 5.0, need=1.0),
            _p("hurt", "Hobbled Back", "RB", 5.1, need=1.0,
               injury="Doubtful", fit_reasons=["listed Doubtful"],
               notes={"injury": "Doubtful — Hamstring",
                      "severity": "unlikely to play"})],
        "context": _ctx(5, {"RB": 1}, _roster("QB", "WR", "WR", "TE")),
        "want": "fit",
    },
    {
        "id": "empty-slot-beats-a-ninth-back",
        "kind": "must",
        "why": "The nine-consecutive-RBs failure, distilled. Every skill slot "
               "is full except TE; another back cannot take the field.",
        "candidates": [_p("rb9", "Ninth Back", "RB", 7.0, need=-2.5),
                       _p("te", "Only Tight End", "TE", 3.0, need=2.0)],
        "context": _ctx(7, {"TE": 1},
                        _roster(*(["RB"] * 5 + ["WR", "WR", "QB"]))),
        "want": "te",
    },
    {
        "id": "bye-week-breaks-a-tie",
        "kind": "prefer",
        "why": "Identical players; one lands on a week where three starters "
               "already sit. Adding a fourth costs that week outright.",
        "candidates": [_p("crowded", "Week Six Man", "WR", 5.0, need=1.0,
                          bye=6),
                       _p("clear", "Week Eleven Man", "WR", 5.0, need=1.0,
                          bye=11)],
        "context": _ctx(6, {"WR": 1}, _roster("QB", "RB", "RB", "TE"),
                        bye_counts={"6": 3, "11": 0}),
        "want": "clear",
    },
    {
        "id": "market-bargain",
        "kind": "prefer",
        "why": "A player the market rates 40 picks higher than where we are "
               "is value the board's own numbers do not capture.",
        "candidates": [_p("bargain", "Slid Too Far", "WR", 5.0, need=1.0,
                          market_rank=20),
                       _p("reach", "Priced Right", "WR", 5.3, need=1.0,
                          market_rank=95)],
        "context": _ctx(6, {"WR": 1}, _roster("QB", "RB", "RB", "TE")),
        "want": "bargain",
    },
]


def ollama_post(body):
    req = urllib.request.Request(OLLAMA_URL + "/api/chat", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.load(r)["message"]["content"]


def claude_post(body):
    import anthropic
    payload = json.loads(body)
    client = anthropic.Anthropic()
    resp = client.messages.create(
        model=CLAUDE_MODEL, max_tokens=300,
        system=payload["messages"][0]["content"],
        messages=[{"role": "user", "content": payload["messages"][1]["content"]}])
    return "".join(b.text for b in resp.content if b.type == "text").strip()


def run_case(case, post_fn, repeat=1):
    """Returns (passes, attempts, [(chosen_name, reason), ...])."""
    got, passes = [], 0
    for _ in range(repeat):
        d = agent.decide_one(case["candidates"], context=case["context"],
                             with_explanation=False, _post_fn=post_fn)
        key = (d["candidate"] or {}).get("player_key")
        got.append(((d["candidate"] or {}).get("name"), d["reason"]))
        if "want" in case:
            passes += 1 if key == case["want"] else 0
        else:
            passes += 1 if key not in case["not_want"] else 0
    return passes, repeat, got


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--with-claude", action="store_true")
    ap.add_argument("--only", default=None)
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--verbose", action="store_true")
    a = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    cases = [c for c in CASES if not a.only or c["id"] == a.only]
    runners = [("local", ollama_post)]
    if a.with_claude:
        runners.append(("claude", claude_post))

    failed_hard = 0
    for who, post in runners:
        hard = soft = hard_n = soft_n = 0
        print(f"\n=== {who} ===")
        for case in cases:
            passes, n, got = run_case(case, post, a.repeat)
            ok = passes == n
            tag = "ok  " if ok else ("FAIL" if case["kind"] == "must"
                                     else "miss")
            score = f"{passes}/{n}" if n > 1 else ""
            print(f"  [{tag}] {case['id']:32} {case['kind']:6} {score}")
            if not ok or a.verbose:
                print(f"         took: {got[0][0]}")
                print(f"         said: {got[0][1][:96]}")
                if not ok:
                    print(f"         why it matters: {case['why']}")
            if case["kind"] == "must":
                hard_n += 1
                hard += 1 if ok else 0
            else:
                soft_n += 1
                soft += 1 if ok else 0
        print(f"  HARD {hard}/{hard_n}   soft {soft}/{soft_n}")
        if who == "local":
            failed_hard = hard_n - hard

    print("\nHARD cases are bugs when they fail; soft cases are preferences "
          "where reasonable drafters differ. The exit code follows HARD only.")
    return 1 if failed_hard else 0


if __name__ == "__main__":
    sys.exit(main())
