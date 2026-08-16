r"""Replay a COMPLETED draft and compare pick-makers head to head.

    & C:\Users\awarm\wes-pc\.venv\Scripts\python.exe C:\Users\awarm\wes\tests\draft_replay.py <draft_id> <slot>

For every pick our slot made, rebuild the board EXACTLY as it stood at that
moment (from the pick prefix) and ask each contender to choose from the same
shortlist. Offline, repeatable, no live draft required, no risk.

THE QUESTION THIS ANSWERS IS NOT "12b OR CLAUDE"
It is **"does LLM judgment beat the sort at all?"** The engine's own top pick is
included as a contender for exactly that reason. If neither model beats
value-over-replacement, the judgment layer is not earning its cost and the model
choice is moot — which is worth knowing before wiring either into a live draft.

What it CANNOT tell you: whether a pick was actually good. There is no ground
truth here — the CPU's real pick is shown for reference, not as an answer. This
measures agreement and reveals reasoning, so a human can judge the reads. It is
a comparison harness, not a scoreboard.
"""
import argparse
import json
import os
import sys
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "pc"))

import wes_draft_agent as agent  # noqa: E402
import wes_sleeper as sl  # noqa: E402

OLLAMA_URL = os.environ.get("WES_OLLAMA_URL", "http://127.0.0.1:11434")
LOCAL_MODEL = os.environ.get("WES_ESCALATE_MODEL", "gemma4:12b")
CLAUDE_MODEL = os.environ.get("WES_REPLAY_CLAUDE", "claude-haiku-4-5-20251001")


def ollama_post(body):
    req = urllib.request.Request(OLLAMA_URL + "/api/chat", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.load(r)["message"]["content"]


def claude_post(body):
    """Same contract as ollama_post: takes the ollama-shaped request body,
    returns the model's JSON string. Translating here keeps `agent.choose`
    ignorant of which backend it is talking to."""
    import anthropic
    payload = json.loads(body)
    system = payload["messages"][0]["content"]
    user = payload["messages"][1]["content"]
    client = anthropic.Anthropic()
    resp = client.messages.create(
        model=CLAUDE_MODEL, max_tokens=300, system=system,
        messages=[{"role": "user", "content": user}])
    return "".join(b.text for b in resp.content if b.type == "text").strip()


def replay(draft_id, slot, league_id, limit=8, contenders=("engine", "local")):
    picks = sl.draft_picks(draft_id) or []
    if not picks:
        return f"draft {draft_id} has no picks to replay"
    mine = [i for i, p in enumerate(picks) if p.get("draft_slot") == slot]
    if not mine:
        return f"slot {slot} made no picks in that draft"

    rows = []
    for i in mine:
        history = picks[:i]                     # the board as it then stood
        actual = picks[i]
        state = sl.draft_candidates(league_id, draft_id, slot, limit=limit,
                                    _picks=history)
        if isinstance(state, str) or not state.get("candidates"):
            continue
        cands = state["candidates"]
        row = {"pick_no": actual.get("pick_no"), "round": actual.get("round"),
               "actual": (actual.get("metadata") or {}).get("last_name")
               or actual.get("player_id"),
               "shortlist": [c["name"] for c in cands]}

        # THE SAME CONTEXT THE LIVE LOOP PASSES. Replaying with none measures a
        # different agent from the one that drafts — choosing with no roster in
        # view is exactly what produced nine consecutive running backs, and a
        # harness reproducing the bug-era inputs would report on a system nobody
        # runs (2026-08-15).
        ctx = {"round": state.get("round"),
               "pick_number": actual.get("pick_no"),
               "picks_until_next_turn": state.get("picks_until_turn"),
               "starting_slots": state.get("starting_slots"),
               "roster_so_far": state.get("roster"),
               "still_unfilled": state.get("still_unfilled"),
               "bye_counts": state.get("bye_counts"),
               "phase": state.get("phase")}

        for who in contenders:
            if who == "engine":
                row["engine"] = (cands[0]["name"], "top of the board")
                continue
            post = ollama_post if who == "local" else claude_post
            try:
                pick, reason, source = agent.choose(cands, context=ctx,
                                                    _post_fn=post)
                row[who] = (pick["name"],
                            reason if source == "model" else f"FELL BACK: {reason}")
            except Exception as e:  # noqa: BLE001 — one model failing must not
                row[who] = (None, f"error: {type(e).__name__}")   # end the run
        rows.append(row)
    return rows


def report(rows, contenders):
    if isinstance(rows, str):
        return rows
    out = [f"Replayed {len(rows)} picks.", ""]
    agree = {c: 0 for c in contenders if c != "engine"}
    for r in rows:
        out.append(f"--- pick {r['pick_no']} (round {r['round']}) — "
                   f"CPU actually took {r['actual']}")
        for who in contenders:
            name, why = r.get(who, (None, "n/a"))
            out.append(f"    {who:8} {str(name):24} {why[:70]}")
        for who in agree:
            if r.get(who, (None,))[0] == r.get("engine", (None,))[0]:
                agree[who] += 1
    out.append("")
    for who, n in agree.items():
        pct = 100.0 * n / len(rows) if rows else 0
        out.append(f"{who} agreed with the engine's top pick on "
                   f"{n}/{len(rows)} picks ({pct:.0f}%)")
    out.append("")
    out.append("NOTE: agreement is not accuracy. There is no ground truth for a "
               "draft pick; this shows how often judgment DIFFERS from the sort, "
               "and what it says when it does, so a human can judge the reads.")
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("draft_id")
    ap.add_argument("slot", type=int)
    ap.add_argument("--league", default="1393935116232818688",
                    help="league whose SCORING to use (a mock has none)")
    ap.add_argument("--with-claude", action="store_true")
    ap.add_argument("--limit", type=int, default=8)
    a = ap.parse_args()

    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    contenders = ["engine", "local"] + (["claude"] if a.with_claude else [])
    rows = replay(a.draft_id, a.slot, a.league, limit=a.limit,
                  contenders=tuple(contenders))
    print(report(rows, contenders))


if __name__ == "__main__":
    main()
