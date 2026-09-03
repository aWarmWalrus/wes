r"""The draft loop: watch the clock, decide, pick, verify (ticket #039).

    & C:\Users\awarm\wes-pc\.venv\Scripts\python.exe ^
        -m sleeper.draft_run <draft_id> <roster_id> [--league <id>]

(with C:\Users\awarm\wes\pc on PYTHONPATH. Normally you want draft_day, which
pre-flights and waits for the room before handing off to this.)

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

from sleeper import chat_context as draft_chat_context
from sleeper import reporting as draft_reporting
from sleeper import banter as wes_banter
import wes_browser
import wes_draft
from sleeper import agent as wes_draft_agent
import wes_execute
from sleeper import shortlist as wes_shortlist
from sleeper import data as wes_sleeper

# How often to look. Fast when our pick is near, lazy when it is not — a draft
# can run for hours and there is nothing to do for most of it.
POLL_NEAR_S = 5.0
# 8s, down from 20s. This is the dominant term in how long somebody waits for
# an answer: a chat read on the held browser is sub-second and the model call
# is 6-11s, so a 20s poll meant a direct question could sit unseen for longer
# than everything else combined. The reads themselves are cheap -- draft_turn
# is cached at 3s and a chat read reuses the open page -- and the near-clock
# cadence has been 5s all along without trouble.
POLL_FAR_S = 8.0
# Within this many picks of our turn, poll faster. It used to ALSO gate the
# chat -- no reads inside the window at all -- which was right when a read cost
# a browser launch and merely conservative once the held page made it
# sub-second. It is now purely a cadence switch.
NEAR_PICKS = 2

# How many times to attempt a pick while the clock is still ours. The failures
# seen live are transient -- a button that reads disabled and then does not, a
# commit that takes fifteen seconds -- and one attempt then standing down hands
# the turn to autopick, which then takes every later turn as well.
SUBMIT_TRIES = 3


def _log(msg):
    print(f"[draft] {time.strftime('%H:%M:%S')} {msg}", flush=True)


# How often to confirm AUTO-PICK is still off, in seconds. Sleeper turns it on
# by itself after ONE missed pick and then takes every subsequent turn
# instantly -- so the checks at session build and at pick time are both on
# paths autopick prevents us from reaching. It has to be checked while we are
# merely WAITING, which is the only state autopick leaves us in.
AUTOPICK_CHECK_S = 45.0


def _keep_autopick_off(browser, last_checked, now, log):
    """Confirm AUTO-PICK is off. Returns the new last-checked time.

    Cheap: one evaluate() against a page we are already holding. Silent when
    all is well, loud when it had to intervene -- an agent that quietly undoes
    something the platform did is one you cannot debug later."""
    if browser is None or now - last_checked < AUTOPICK_CHECK_S:
        return last_checked
    try:
        # RELOAD BEFORE READING. The toggle's DOM state is whatever it was
        # when the page loaded: a session opened before autopick engaged shows
        # OFF forever, which is exactly what happened -- the guard ran, saw a
        # stale OFF, reported nothing, and autopick took every one of our
        # turns (2026-08-22, second live draft). A check that cannot observe
        # the thing it guards is worse than no check, because it reassures.
        if browser.peek() is None:
            return last_checked          # nothing built yet, nothing to check
        page = browser.refresh()
        state = wes_sleeper.autopick_on(page)
        if state is None:
            # UNKNOWN IS NOT OFF. A missing control means we could not read
            # it, and reading that as "fine" is what let autopick run an
            # entire draft while this function reported nothing.
            log("could not read the AUTO-PICK toggle — treating as UNKNOWN, "
                "will retry")
            return last_checked          # retry next cycle rather than assume
        if state:
            got = wes_sleeper.set_autopick(page, False)
            log(f"AUTO-PICK had switched itself ON — turned it "
                f"{'off' if got is False else f'to {got!r}'}")
    except Exception as e:  # noqa: BLE001 — never break a draft over this
        log(f"autopick check failed: {type(e).__name__}: {e}")
    return now


def _shut(browser, log):
    """Close the held session and say what it cost. The counters are the only
    evidence of whether holding a page for two hours was a good idea."""
    if browser is None:
        return
    log(f"browser: {browser.stats()}")
    browser.close()


def _banter_context(draft_id, league_id, roster_id, state, wait,
                    board_fn, shortlist=None):
    """Kept as a name here because the loop and its tests both use it;
    the assembly itself lives in draft_chat_context, which is a payload
    for a different agent and was a hundred and forty lines of paging
    past."""
    return draft_chat_context.build(draft_id, league_id, roster_id,
                                    state, wait, board_fn, shortlist)


def run(draft_id, league_id, roster_id, max_seconds=6 * 3600,
        _state_fn=None, _decide_fn=None, _submit_fn=None, _sleep_fn=None,
        _now_fn=None, _board_fn=None, _ledger_path=None, _record_fn=None,
        banter_mode="off", banter_gap=None, _banter=None, _explain_fn=None):
    """Watch and draft until the draft ends. Returns a short summary string."""
    turn_fn = _state_fn or wes_sleeper.draft_turn
    board_fn = _board_fn or wes_sleeper.draft_candidates
    _raw_submit = _submit_fn or wes_sleeper.submit_pick
    sleep = _sleep_fn or time.sleep
    now = _now_fn or time.time
    record = _record_fn or draft_reporting._record
    # Fills a decision's rationale AFTER the click, off the clock. Injectable
    # so a test can assert it ran without reaching a model; it is also
    # idempotent, so a caller supplying its own explained decision is untouched.
    explain_fn = _explain_fn or wes_draft_agent.attach_explanation
    # Banter shares this process ON PURPOSE. The Chrome profile is a persistent
    # singleton, so a second process reading the chat would collide with the
    # one submitting picks. Only touched when we are NOT on the clock.
    # ONE BROWSER for the whole draft. Chat was paying a ~9s launch on
    # almost every poll to fetch a handful of messages; a held page makes a
    # read sub-second. It recycles itself on a failed health check, and every
    # caller degrades to a per-call session if it cannot be built -- a speed
    # optimisation must never be able to stop a draft.
    browser = None
    if _banter is not None and getattr(_banter, "browser", None) is not None:
        # An injected banter that already holds a page owns it. Building a
        # second here is the same "two Playwright instances" bug in a
        # different costume.
        browser = _banter.browser
    else:
        # ALWAYS, not just for banter. The AUTO-PICK guard needs a page it can
        # look at while merely waiting, and holding one also takes ~7s off
        # every pick. Degrades to per-call sessions if it cannot be built.
        try:
            browser = wes_browser.Browser(draft_id, _log=_log)
        except Exception as e:  # noqa: BLE001
            _log(f"browser: could not hold a session ({e}); per-call it is")
    # THE HELD BROWSER MUST REACH THE PICK PATH TOO. Threading it into banter
    # and not here meant a pick opened a SECOND Playwright while the first was
    # alive, which fails with "Sync API inside the asyncio loop" and forfeits
    # the pick. Wrapped rather than passed positionally so injected test stubs
    # keep their three-argument signature.
    # OUR SLOT reaches submit_pick so its verification can require that WE
    # made the pick. Without it, another manager taking the same player
    # satisfies the check and a missed click reports success.
    _slot_holder = {"slot": None}

    if _submit_fn is None:
        def submit(d, key, name):
            return _raw_submit(d, key, name, browser=browser,
                               slot=_slot_holder["slot"])
    else:
        submit = _raw_submit

    chat = _banter if _banter is not None else wes_banter.Banter(
        draft_id, mode=banter_mode, browser=browser,
        min_gap_s=wes_banter.MIN_GAP_S if banter_gap is None else banter_gap)

    # WARM IT NOW. Building on first use cost pick 2 of a live draft: five CPU
    # teams picked within seconds of the start, and we were still launching
    # Chrome when our turn came and went. A session built during the quiet
    # period costs nothing and is the difference between being ready and
    # being late.
    # `_submit_fn is None` means we are really drafting, not under test. An
    # injected submit is the existing signal for that (see the wrapper above),
    # and without this the suite launches a real Chrome per test -- 23s became
    # 215s, which is how the last one of these got noticed.
    if browser is not None and _submit_fn is None and _state_fn is None:
        try:
            browser.page()
        except Exception as e:  # noqa: BLE001 — falls back to per-call
            _log(f"browser: could not pre-warm ({e})")

    started = now()
    acted_on = set()
    made = []
    last_wait = None
    last_autopick_check = 0.0
    # THE SHARED SHORTLIST. Written on the clock by the drafting path, read
    # between picks by the chat. One statement of what we want, rather than
    # two agents guessing at the same board from opposite ends.
    shortlist = wes_shortlist.Shortlist()

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

        _slot_holder["slot"] = state.get("my_slot")
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
                 f"({draft_reporting._roster_line(board_fn, league_id, draft_id, roster_id)})")
        # EVERY CYCLE while waiting, not just before a pick. This is the only
        # place the check can live: once autopick is on, our turn is taken
        # before we ever reach the pick path.
        last_autopick_check = _keep_autopick_off(
            browser, last_autopick_check, now(), _log)

        if wait > 0:
            # CHAT AT ANY DISTANCE FROM THE CLOCK. This used to stop inside
            # NEAR_PICKS on the grounds that "a read takes ~12s of browser" --
            # true when every read paid a full browser launch, and obsolete
            # since the held page made a read sub-second. Measured: the quiet
            # and rate-limited paths return in ~0.00s and 0.05s.
            #
            # What is left to spend is the model call, and only when there is
            # something new AND the floor has passed -- at most once per 30-60s
            # by construction. Against a 120-600s pick clock that is affordable,
            # and being unable to answer somebody in the two picks before our
            # turn is exactly when a draft room is most worth talking in.
            _t0 = now()
            act, detail = chat.tick(context=_banter_context(
                draft_id, league_id, roster_id, state, wait, board_fn,
                shortlist=shortlist))
            _took = now() - _t0
            # Log the QUIET decisions too when there was actually something to
            # answer. "nothing worth saying (re: ...)" is the interesting line;
            # suppressing it made an idle chat and a model declining to speak
            # look identical, which is exactly the question asked five minutes
            # after switching it on.
            if act not in ("quiet", "rate_limited") or "(re: " in detail:
                _log(f"chat [{act}] ({_took:.1f}s) {detail}")
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
        # PUBLISH THE BOARD. This is the one place it exists, and until now it
        # was used once and dropped -- so nothing else could know who we were
        # chasing or who got taken out from under us.
        shortlist.note_board(cands, pick_no)
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
        # Keep the shared shortlist honest: a target already gone must not
        # still read as something we are hoping falls to us.
        shortlist.mark_taken(taken)
        cands = [c for c in cands if str(c.get("player_key")) not in taken]
        if not cands:
            _log(f"pick {pick_no}: everyone on the shortlist is gone — "
                 f"standing down")
            acted_on.add(pick_no)
            continue

        # with_explanation=False KEEPS THE SECOND MODEL CALL OFF THE CLOCK.
        # `decide_one` used to make both calls before returning, so the pick and
        # the paragraph about the pick both sat between our turn and the click
        # -- and the TIMING line below reported them as one `model` number,
        # which is why 61s looked like one slow call. The explanation is asked
        # about a pick that is already fixed, so it loses nothing by waiting;
        # `attach_explanation` runs it after the click, below.
        decision = (_decide_fn or wes_draft_agent.decide_one)(
            cands, context=ctx, with_explanation=False)
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
            # A shadow run has no clock to protect and its whole value is the
            # record, so the explanation is worth waiting for here.
            explain_fn(decision, cands, context=ctx)
            record(league_id, roster_id, draft_id, pick_no, board, decision,
                   "would_draft", note="writes are off",
                   _ledger_path=_ledger_path)
            acted_on.add(pick_no)
            continue

        acted_on.add(pick_no)          # marked BEFORE the attempt, deliberately

        def _already_ours():
            """Did OUR CHOSEN PLAYER land after all?

            THE PLAYER, not merely the slot. The first version asked only
            whether our pick number had been filled by our slot -- so when a
            click failed and autopick filled the turn with somebody else, it
            answered yes, the retry broke out as success, and the log printed
            the name we had WANTED. That reported drafting Bijan Robinson while
            he went to another manager and our pick was Jahmyr Gibbs
            (2026-08-22). It bypassed the slot-scoped verification standing
            right next to it."""
            try:
                for pk in (wes_sleeper.draft_picks(draft_id) or []):
                    if pk.get("pick_no") != pick_no:
                        continue
                    return (str(pk.get("player_id")) == str(pick["player_key"])
                            and int(pk.get("draft_slot") or -1) == int(
                                _slot_holder["slot"] or -2))
            except Exception:  # noqa: BLE001
                pass
            return False

        def _still_ours():
            """Is the clock still on us? If it has moved on, stop trying."""
            st = turn_fn(draft_id, roster_id)
            return (not isinstance(st, str)
                    and st.get("picks_until_turn") == 0
                    and st.get("picks_made", -1) + 1 == pick_no)

        try:
            # RETRY WHILE THE CLOCK IS OURS. One attempt then stand down was
            # right when a failed verification might mean a pick had silently
            # landed -- retrying would have risked a double pick. It cannot
            # now: verification requires the pick's slot to be OURS, so a
            # second attempt after a genuine miss is safe, and _already_ours()
            # catches the case where the first one worked late.
            #
            # This matters because the failures are TRANSIENT: a button that
            # reads disabled for twenty seconds and then is not, a commit that
            # takes fifteen seconds, a room mid-render. Standing down on the
            # first of those hands the pick to autopick -- which then takes
            # every later turn too (2026-08-22).
            for attempt in range(SUBMIT_TRIES):
                try:
                    submit(draft_id, pick["player_key"], pick["name"])
                    break
                except Exception as e:  # noqa: BLE001
                    if _already_ours():
                        _log(f"pick {pick_no}: landed after all (slow commit)")
                        break
                    if attempt == SUBMIT_TRIES - 1 or not _still_ours():
                        raise
                    # A REFRESH IS NOT ENOUGH. Measured on a stalled turn: the
                    # held page read the draft button as disabled for three
                    # attempts while a BRAND NEW session, opened seconds later
                    # against the same room, read it as enabled. A page built
                    # before the draft opened carries that state through a
                    # reload, so the retry throws the session away instead.
                    if browser is not None:
                        browser.close()
                        _log(f"pick {pick_no}: attempt {attempt + 1} failed "
                             f"({str(e)[:60]}); discarding the session and "
                             f"retrying fresh")
                    else:
                        _log(f"pick {pick_no}: attempt {attempt + 1} failed "
                             f"({str(e)[:70]}); clock is still ours, retrying")
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
            #
            # ASKED FOR HERE, after the click and after the TIMING line, so it
            # costs the pick nothing. The log and the ledger below read exactly
            # the same as when `decide_one` produced it inline.
            #
            # GUARDED SEPARATELY, and this is not belt-and-braces. The pick has
            # ALREADY LANDED at this point, so an exception escaping here would
            # fall into the handler below and record a successful pick as
            # "failed" -- a false entry in the ledger about a real roster. A
            # missing paragraph must never be able to say that.
            try:
                explain_fn(decision, cands, context=ctx)
            except Exception as ex:  # noqa: BLE001 — see above
                _log(f"pick {pick_no}: explanation failed ({ex}) — the pick "
                     f"stands")
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
                    default="auto",
                    help="chat in the draft room. Default 'auto' (posts); "
                         "'propose' composes and logs without posting, which "
                         "is the right mode for a room of strangers since "
                         "messages go under the owner's name; 'off' is "
                         "silent.")
    a = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    _log(f"watching draft {a.draft_id} for roster {a.roster_id} "
         f"(writes {'ON' if wes_execute.writes_enabled() else 'OFF'})")
    print(run(a.draft_id, a.league, a.roster_id, max_seconds=a.max_seconds,
              banter_mode=a.banter, banter_gap=a.banter_gap))


if __name__ == "__main__":
    main()
