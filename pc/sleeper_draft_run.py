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
               "still_unfilled": board.get("still_unfilled"),
               # Bye exposure and which draft we are in. Without these the
               # model was told to weigh bye spread with no byes in hand, and
               # had nothing to reason with once the starters were full — the
               # last seven picks of a clean mock were all RBs, justified in
               # the same sentence five times (2026-08-15).
               "bye_counts": board.get("bye_counts"),
               "phase": board.get("phase"),
               # What the room has been taking. The prompt has asked the model
               # to weigh positional runs since the first version while showing
               # it none — the same omission as the byes, in a second place.
               "recent_picks_by_position":
                   board.get("recent_picks_by_position")}
        # Re-verify availability against FRESH picks — cache BYPASSED. The
        # board was true a moment ago, and a moment is enough for someone to
        # take him; reading a 15s-old pick list here is not a re-check at all.
        taken = wes_sleeper.drafted_player_ids_fresh(draft_id)
        cands = [c for c in cands if str(c.get("player_key")) not in taken]
        if not cands:
            _log(f"pick {pick_no}: everyone on the shortlist is gone — "
                 f"standing down")
            acted_on.add(pick_no)
            continue

        decision = (_decide_fn or wes_draft_agent.decide_one)(
            cands, context=ctx)
        pick = decision["candidate"]
        reason, source = decision["reason"], decision["source"]
        if pick is None:
            _log(f"pick {pick_no}: no decision — standing down")
            acted_on.add(pick_no)
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
            # The full rationale, not just the sentence: the factors weighed
            # and the player nearly taken are what make a draft reviewable
            # afterwards, and this agent has already been caught narrating a
            # check it did not perform.
            _log(f"pick {pick_no}: DRAFTED "
                 f"{wes_draft_agent.format_decision(decision)}")
        except Exception as e:  # noqa: BLE001 — one failure must not end the run
            # TWO KINDS OF FAILURE, and only one is dangerous.
            #
            # "not in the draft room's available list" happens BEFORE any click,
            # so nothing was submitted and the next candidate is safe to try.
            # Treating it like an uncertain write forfeited nine picks in one
            # draft (2026-08-15).
            #
            # Anything else may have half-landed. submit_pick already polled
            # ~15s, so retrying is a coin flip on whether the first attempt
            # arrived late — and that coin flip is a double pick. Stand down and
            # let cpu_autopick have it.
            if "not available in the draft room" in str(e):
                alt = next((c for c in cands
                            if c["player_key"] != pick["player_key"]), None)
                if alt is not None:
                    # LOUDLY. Substituting is correct only when the player is
                    # genuinely gone, and a quiet substitution is worse than a
                    # forfeit: seven silent swaps produced a roster with four
                    # tight ends and no defence, and nothing in the log said the
                    # agent had not got what it asked for (2026-08-15).
                    _log(f"pick {pick_no}: SUBSTITUTED — wanted "
                         f"{pick['name']}, he is gone; taking {alt['name']} "
                         f"instead")
                    try:
                        submit(draft_id, alt["player_key"], alt["name"])
                        made.append(alt["name"])
                        _log(f"pick {pick_no}: DRAFTED {alt['name']} (fallback)")
                        continue
                    except Exception as e2:  # noqa: BLE001
                        e = e2
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
