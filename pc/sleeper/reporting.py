"""Recording a draft decision, and describing the roster it built.

SPLIT OUT OF sleeper_draft_run, which is the LOOP. Neither of these is loop
logic: one writes a durable row to the shared fantasy ledger, the other renders
a one-line roster summary for the heartbeat. Both were read past constantly to
get at the pick rails underneath.
"""
import time

import wes_execute


def _record(league_id, roster_id, draft_id, pick_no, board, decision,
            outcome, drafted=None, note="", _ledger_path=None, _now=None):
    """Write one draft decision to the shared fantasy ledger.

    WHY THE LEDGER AND NOT JUST THE LOG. A draft is reviewed afterwards, and a
    log file is a thin place to keep the only record of fifteen irreversible
    decisions — grep-able at best, gone at worst. The ledger is structured,
    already holds every Yahoo action, and already backs `fantasy_recent_moves`,
    so "why did it take Zay Flowers over Cam Skattebo?" becomes answerable
    months later instead of being reconstructed from memory.

    EVERY decision is recorded, not just the successful ones. A stand-down and
    a substitution are exactly the rows worth having later — the nine silent
    substitutions that produced four tight ends and no defence were invisible
    precisely because nothing durable said what had been wanted (2026-08-15).

    `action_type` is "draft_pick", which `count_recent_moves` ignores, so these
    rows cannot eat the weekly add/drop budget."""
    cand = decision.get("candidate") or {}
    entry = {
        "ts": _now if _now is not None else time.time(),
        # Platform-qualified so a Sleeper roster can never collide with a
        # Yahoo team key in the same file.
        "team_key": f"sleeper.l.{league_id}.r.{roster_id}",
        "name": f"Sleeper roster {roster_id}",
        "sport": "nfl", "action_type": "draft_pick", "autonomy": "auto",
        "draft_id": draft_id, "pick_no": pick_no,
        "round": (board or {}).get("round"),
        "phase": (board or {}).get("phase"),
        # What it WANTED, which is not always what it got.
        "moves": [{"add": cand.get("name"), "player_key": cand.get("player_key"),
                   "position": (cand.get("positions") or [None])[0],
                   "nfl_team": cand.get("team"), "bye": cand.get("bye"),
                   "vor": cand.get("vor"),
                   "market_rank": cand.get("market_rank")}] if cand else [],
        "why": decision.get("reason"),
        "source": decision.get("source"),
        "considered": decision.get("considered") or [],
        "runner_up": decision.get("runner_up"),
        "why_not": decision.get("why_not"),
        # The choice set, so a later review can ask what it passed over -- the
        # shortlist IS half the decision, and without it "took the best
        # available" is unfalsifiable.
        "shortlist": [c.get("name") for c in ((board or {}).get("candidates")
                                              or [])],
        "outcome": outcome,
        "executed": outcome in ("drafted", "substituted"),
        "dry_run": outcome == "would_draft",
    }
    if drafted and drafted != cand.get("name"):
        entry["actually_drafted"] = drafted
    if note:
        entry["note"] = note
    wes_execute.record_action(entry, _ledger_path)


_ROSTER_CACHE = {}


def _roster_line(board_fn, league_id, draft_id, roster_id):
    """"3 RB, 2 WR, 1 QB" — what we hold, at a glance.

    Cached on our own pick count: the board is the expensive call and a
    heartbeat must never cost what a decision costs."""
    try:
        key = (draft_id, roster_id)
        made = len(_ROSTER_CACHE.get(key, {}).get("roster", []))
        board = board_fn(league_id, draft_id, roster_id, limit=1)
        if isinstance(board, str):
            return "roster unknown"
        _ROSTER_CACHE[key] = board
        counts = {}
        for r in board.get("roster") or []:
            pos = r.get("position") or "?"
            counts[pos] = counts.get(pos, 0) + 1
        if not counts:
            return "no picks yet"
        held = ", ".join(f"{n} {p}" for p, n in sorted(counts.items(),
                                                       key=lambda x: -x[1]))
        gaps = board.get("still_unfilled") or {}
        need = ("; need " + ", ".join(f"{p}x{n}" for p, n in gaps.items())
                if gaps else "; starters full")
        return held + need
    except Exception:  # noqa: BLE001 — a heartbeat must never break a draft
        return "roster unknown"
