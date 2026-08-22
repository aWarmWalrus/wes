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

import wes_banter
import wes_browser
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


def _shut(browser, log):
    """Close the held session and say what it cost. The counters are the only
    evidence of whether holding a page for two hours was a good idea."""
    if browser is None:
        return
    log(f"browser: {browser.stats()}")
    browser.close()


def _banter_context(draft_id, league_id, roster_id, state, wait, board_fn):
    """What the room actually looks like, for trash talk that lands.

    Banter had `{round, picks_made, picks_until_our_turn}` and nothing about
    who drafted what, so it could only produce generic ribbing. "That is your
    fourth tight end" needs the picks; "your RB1 is on PUP with an ACL" needs
    the notes. Both are things we already hold.

    Best-effort: a chat line is never worth breaking a draft for."""
    ctx = {"round": state.get("round"),
           "picks_made": state.get("picks_made"),
           "picks_until_our_turn": wait}
    try:
        import wes_notes
        import wes_snapshot
        idx = wes_snapshot.players()
        picks = wes_sleeper.draft_picks(draft_id) or []
        # WHO TOOK WHAT, lately. The last handful is what the room is still
        # talking about.
        ctx["recent_picks"] = [
            {"pick": p.get("pick_no"), "slot": p.get("draft_slot"),
             "player": " ".join(filter(None, [
                 (p.get("metadata") or {}).get("first_name"),
                 (p.get("metadata") or {}).get("last_name")])),
             "position": (p.get("metadata") or {}).get("position")}
            for p in picks[-6:]]
        # EVERY team's shape, so it can needle the right person.
        rosters = {}
        for p in picks:
            pos = (p.get("metadata") or {}).get("position")
            if pos:
                rosters.setdefault(p.get("draft_slot"), []).append(pos)
        ctx["rosters_by_slot"] = {
            str(k): _count(v) for k, v in sorted(rosters.items())
            if k is not None}
        ctx["our_slot"] = roster_id
        # OUR injured players, in words. The most quotable facts in the room
        # are usually about a body part.
        hurt = []
        for p in picks:
            if p.get("draft_slot") != roster_id:
                continue
            info = idx.get(str(p.get("player_id"))) or {}
            note = wes_notes.injury_note(info)
            if note:
                hurt.append(f"{info.get('name')}: {note}")
        if hurt:
            ctx["our_injuries"] = hurt
    except Exception:  # noqa: BLE001 — a chat line is not worth a draft
        pass
    return ctx


def _count(items):
    out = {}
    for i in items:
        out[i] = out.get(i, 0) + 1
    return out


def run(draft_id, league_id, roster_id, max_seconds=6 * 3600,
        _state_fn=None, _decide_fn=None, _submit_fn=None, _sleep_fn=None,
        _now_fn=None, _board_fn=None, _ledger_path=None, _record_fn=None,
        banter_mode="off", banter_gap=None, _banter=None):
    """Watch and draft until the draft ends. Returns a short summary string."""
    turn_fn = _state_fn or wes_sleeper.draft_turn
    board_fn = _board_fn or wes_sleeper.draft_candidates
    submit = _submit_fn or wes_sleeper.submit_pick
    sleep = _sleep_fn or time.sleep
    now = _now_fn or time.time
    record = _record_fn or _record
    # Banter shares this process ON PURPOSE. The Chrome profile is a persistent
    # singleton, so a second process reading the chat would collide with the
    # one submitting picks. Only touched when we are NOT on the clock.
    # ONE BROWSER for the whole draft. Chat was paying a ~9s launch on
    # almost every poll to fetch a handful of messages; a held page makes a
    # read sub-second. It recycles itself on a failed health check, and every
    # caller degrades to a per-call session if it cannot be built -- a speed
    # optimisation must never be able to stop a draft.
    browser = None
    if banter_mode != "off":
        try:
            browser = wes_browser.Browser(draft_id, _log=_log)
        except Exception as e:  # noqa: BLE001
            _log(f"browser: could not hold a session ({e}); per-call it is")
    chat = _banter if _banter is not None else wes_banter.Banter(
        draft_id, mode=banter_mode, browser=browser,
        min_gap_s=wes_banter.MIN_GAP_S if banter_gap is None else banter_gap)

    started = now()
    acted_on = set()
    made = []
    last_wait = None

    while True:
        if now() - started > max_seconds:
            _shut(browser, _log)
            return f"stopped after {max_seconds}s; made {len(made)} pick(s)"

        state = turn_fn(draft_id, roster_id)
        if isinstance(state, str):
            # Includes the normal end condition ("that draft is over").
            if "over" in state.lower():
                _shut(browser, _log)
                return f"draft finished; made {len(made)} pick(s)"
            _log(f"state unavailable: {state}")
            sleep(POLL_FAR_S)
            continue

        wait = state.get("picks_until_turn")
        if wait is None:
            _shut(browser, _log)
            return f"our draft is done; made {len(made)} pick(s)"
        # A HEARTBEAT, so the log says something between picks. Without it the
        # gap between our turns is indistinguishable from a hung process --
        # and in a draft with no clock, hung is a real possibility nobody
        # would notice.
        if wait != last_wait:
            last_wait = wait
            _log(f"round {state.get('round', '?')}, {state.get('picks_made')} "
                 f"picks in — {wait} until our turn "
                 f"({_roster_line(board_fn, league_id, draft_id, roster_id)})")
        if wait > 0:
            # Far from the clock is the only safe time to open the chat: a
            # read takes ~12s of browser, and a pick must never wait behind it.
            if wait > NEAR_PICKS:
                act, detail = chat.tick(context=_banter_context(
                    draft_id, league_id, roster_id, state, wait, board_fn))
                # Log the QUIET decisions too when there was actually
                # something to answer. "nothing worth saying (re: ...)" is the
                # interesting line; suppressing it made an idle chat and a
                # model declining to speak look identical, which is exactly
                # the question asked five minutes after switching it on.
                if act not in ("quiet", "rate_limited") or "(re: " in detail:
                    _log(f"chat [{act}] {detail}")
            sleep(POLL_NEAR_S if wait <= NEAR_PICKS else POLL_FAR_S)
            continue

        # --- on the clock ---------------------------------------------------
        # THE CLOCK STARTS HERE. Everything from noticing our turn to the
        # button being clicked is time we are spending against the pick timer,
        # and the only honest way to know the margin is to measure it rather
        # than reason about it.
        t_turn = now()
        pick_no = state.get("picks_made", 0) + 1
        if pick_no in acted_on:
            # Already submitted for this pick and the API has not caught up.
            # Waiting is right: acting again is the double-pick.
            sleep(POLL_NEAR_S)
            continue

        # Only NOW pay for the board — this is the expensive call, and it is
        # worth seconds here where it was fatal in the poll.
        board = board_fn(league_id, draft_id, roster_id)
        t_board = now()
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
        t_fresh = now()
        cands = [c for c in cands if str(c.get("player_key")) not in taken]
        if not cands:
            _log(f"pick {pick_no}: everyone on the shortlist is gone — "
                 f"standing down")
            acted_on.add(pick_no)
            continue

        decision = (_decide_fn or wes_draft_agent.decide_one)(
            cands, context=ctx)
        t_decide = now()
        pick = decision["candidate"]
        reason, source = decision["reason"], decision["source"]
        if pick is None:
            _log(f"pick {pick_no}: no decision — standing down")
            record(league_id, roster_id, draft_id, pick_no, board, decision,
                   "stood_down", note="no decision", _ledger_path=_ledger_path)
            acted_on.add(pick_no)
            continue

        if not wes_execute.writes_enabled():
            _log(f"pick {pick_no}: WOULD take {pick['name']} ({source}: "
                 f"{reason}) — writes are off")
            record(league_id, roster_id, draft_id, pick_no, board, decision,
                   "would_draft", note="writes are off",
                   _ledger_path=_ledger_path)
            acted_on.add(pick_no)
            continue

        acted_on.add(pick_no)          # marked BEFORE the attempt, deliberately
        try:
            submit(draft_id, pick["player_key"], pick["name"])
            t_click = now()
            made.append(pick["name"])
            _log(f"pick {pick_no}: TIMING turn->click {t_click - t_turn:.1f}s "
                 f"(board {t_board - t_turn:.1f}s, availability "
                 f"{t_fresh - t_board:.1f}s, model {t_decide - t_fresh:.1f}s, "
                 f"submit {t_click - t_decide:.1f}s)"
                 + (f" [{board.get('tied_count')} tied]"
                    if isinstance(board, dict) and board.get("tied_count")
                    else ""))
            # The full rationale, not just the sentence: the factors weighed
            # and the player nearly taken are what make a draft reviewable
            # afterwards, and this agent has already been caught narrating a
            # check it did not perform.
            _log(f"pick {pick_no}: DRAFTED "
                 f"{wes_draft_agent.format_decision(decision)}")
            record(league_id, roster_id, draft_id, pick_no, board, decision,
                   "drafted", drafted=pick["name"], _ledger_path=_ledger_path)
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
                        record(league_id, roster_id, draft_id, pick_no, board,
                               decision, "substituted", drafted=alt["name"],
                               note=f"{pick['name']} was gone",
                               _ledger_path=_ledger_path)
                        continue
                    except Exception as e2:  # noqa: BLE001
                        e = e2
            record(league_id, roster_id, draft_id, pick_no, board, decision,
                   "failed", note=str(e), _ledger_path=_ledger_path)
            _log(f"pick {pick_no}: FAILED ({e}) — standing down, cpu_autopick "
                 f"will take it")

    # unreachable


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("draft_id")
    ap.add_argument("roster_id", type=int)
    ap.add_argument("--league", default="1393935116232818688")
    ap.add_argument("--max-seconds", type=int, default=6 * 3600)
    ap.add_argument("--banter-gap", type=float, default=None,
                    help="minimum seconds between messages. The floor is not "
                         "the only limiter: chat is only read when more than "
                         "two picks from the clock, so the poll cadence caps "
                         "it too.")
    ap.add_argument("--banter", choices=("off", "propose", "auto"),
                    default="off",
                    help="chat in the draft room. 'propose' composes and logs "
                         "without posting; 'auto' posts. Off by default -- "
                         "messages go to real people under the owner's name.")
    a = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    _log(f"watching draft {a.draft_id} for roster {a.roster_id} "
         f"(writes {'ON' if wes_execute.writes_enabled() else 'OFF'})")
    print(run(a.draft_id, a.league, a.roster_id, max_seconds=a.max_seconds,
              banter_mode=a.banter, banter_gap=a.banter_gap))


if __name__ == "__main__":
    main()
