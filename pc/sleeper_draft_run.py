r"""The draft loop: watch the clock, decide, pick, verify (ticket #039).

    & C:\Users\awarm\wes-pc\.venv\Scripts\python.exe ^
        C:\Users\awarm\wes\pc\sleeper_draft_run.py <draft_id> <roster_id> [--league <id>]

Poll the public draft API (no auth needed for reads), and when our slot comes up,
run the agent's decision and submit the pick through the browser.

THE RAILS, AND WHY EACH ONE EXISTS

* **`cpu_autopick` is the fallback, and it is a GOOD one.** Sleeper will take a
  sane pick if we do nothing. So every failure path here stands down rather than
  retrying into the clock: a missed pick costs a little value, a double pick or a
  half-submitted pick costs a roster spot and a lot of explaining.

* **Never pick twice for the same pick.** The loop derives whose turn it is from
  `len(picks)`, which only advances once a pick is COMMITTED — and `submit_pick`
  polls until it can see ours committed before returning. So the same pick number
  cannot come round twice. `_acted_on` is a second, cheaper belt for the same
  braces, in case a restart lands mid-pick.

* **Re-check availability immediately before submitting.** The shortlist is built
  from a board that was true a moment ago; between deciding and clicking, someone
  else may have taken the player. The candidate is re-verified against fresh
  picks, and a taken player sends us back for a new decision rather than clicking
  a row that no longer means what we think.

* **One attempt per pick.** If a submit fails we do NOT retry it. `submit_pick`
  already polled for ~15s before giving up, so a retry is a coin flip on whether
  the first one landed late — and that coin flip is a double pick.

Everything is injectable so the rails are testable without a live draft.
"""
import argparse
import sys
import time

import wes_draft
import wes_draft_agent
import wes_execute
import wes_sleeper

# How often to look. Fast when our pick is near, lazy when it is not — a draft
# can run for hours and there is nothing to do for most of it.
POLL_NEAR_S = 5.0
POLL_FAR_S = 20.0
NEAR_PICKS = 2


def _log(msg):
    print(f"[draft] {time.strftime('%H:%M:%S')} {msg}", flush=True)


def run(draft_id, league_id, roster_id, max_seconds=6 * 3600,
        _state_fn=None, _decide_fn=None, _submit_fn=None, _sleep_fn=None,
        _now_fn=None, _board_fn=None):
    """Watch and draft until the draft ends. Returns a short summary string."""
    turn_fn = _state_fn or wes_sleeper.draft_turn
    board_fn = _board_fn or wes_sleeper.draft_candidates
    submit = _submit_fn or wes_sleeper.submit_pick
    sleep = _sleep_fn or time.sleep
    now = _now_fn or time.time

    started = now()
    acted_on = set()
    made = []

    while True:
        if now() - started > max_seconds:
            return f"stopped after {max_seconds}s; made {len(made)} pick(s)"

        state = turn_fn(draft_id, roster_id)
        if isinstance(state, str):
            # Includes the normal end condition ("that draft is over").
            if "over" in state.lower():
                return f"draft finished; made {len(made)} pick(s)"
            _log(f"state unavailable: {state}")
            sleep(POLL_FAR_S)
            continue

        wait = state.get("picks_until_turn")
        if wait is None:
            return f"our draft is done; made {len(made)} pick(s)"
        if wait > 0:
            sleep(POLL_NEAR_S if wait <= NEAR_PICKS else POLL_FAR_S)
            continue

        # --- on the clock ---------------------------------------------------
        pick_no = state.get("picks_made", 0) + 1
        if pick_no in acted_on:
            # Already submitted for this pick and the API has not caught up.
            # Waiting is right: acting again is the double-pick.
            sleep(POLL_NEAR_S)
            continue

        # Only NOW pay for the board — this is the expensive call, and it is
        # worth seconds here where it was fatal in the poll.
        board = board_fn(league_id, draft_id, roster_id)
        cands = [] if isinstance(board, str) else (board.get("candidates") or [])
        if not cands:
            _log(f"pick {pick_no}: no candidates — standing down, cpu_autopick "
                 f"will take it")
            acted_on.add(pick_no)
            continue

        # CONTEXT. The `context` parameter existed from the start and was
        # never populated, so the model saw eight players with numbers and no
        # idea what was already on the roster — which is how it took nine
        # running backs and no quarterback (2026-08-15).
        ctx = {"round": board.get("round"),
               "pick_number": pick_no,
               "picks_until_next_turn": board.get("picks_until_turn"),
               "starting_slots": board.get("starting_slots"),
               "roster_so_far": board.get("roster"),
               "still_unfilled": board.get("still_unfilled")}
        pick, reason, source = (_decide_fn or wes_draft_agent.choose)(
            cands, context=ctx)
        if pick is None:
            _log(f"pick {pick_no}: no decision — standing down")
            acted_on.add(pick_no)
            continue

        # Re-verify availability against FRESH picks: the board was true a
        # moment ago, and a moment is enough for someone to take him.
        taken = wes_sleeper.drafted_player_ids(draft_id)
        if str(pick.get("player_key")) in taken:
            _log(f"pick {pick_no}: {pick['name']} was taken while we thought — "
                 f"re-deciding")
            continue

        if not wes_execute.writes_enabled():
            _log(f"pick {pick_no}: WOULD take {pick['name']} ({source}: "
                 f"{reason}) — writes are off")
            acted_on.add(pick_no)
            continue

        acted_on.add(pick_no)          # marked BEFORE the attempt, deliberately
        try:
            submit(draft_id, pick["player_key"], pick["name"])
            made.append(pick["name"])
            _log(f"pick {pick_no}: DRAFTED {pick['name']} ({source}: {reason})")
        except Exception as e:  # noqa: BLE001 — one failure must not end the run
            # No retry, on purpose. submit_pick already polled ~15s; retrying is
            # a coin flip on whether the first attempt landed late, and that coin
            # flip is a double pick. cpu_autopick covers us.
            _log(f"pick {pick_no}: FAILED ({e}) — standing down, cpu_autopick "
                 f"will take it")

    # unreachable


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("draft_id")
    ap.add_argument("roster_id", type=int)
    ap.add_argument("--league", default="1393935116232818688")
    ap.add_argument("--max-seconds", type=int, default=6 * 3600)
    a = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    _log(f"watching draft {a.draft_id} for roster {a.roster_id} "
         f"(writes {'ON' if wes_execute.writes_enabled() else 'OFF'})")
    print(run(a.draft_id, a.league, a.roster_id, max_seconds=a.max_seconds))


if __name__ == "__main__":
    main()
