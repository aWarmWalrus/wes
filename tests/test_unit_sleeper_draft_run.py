"""Unit tests for the draft loop (sleeper_draft_run, #039).

Network-free: state, decision, submit and sleep are all injected, so what is
tested is the RAILS — which is the part that can lose you a roster spot.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "pc"))

import sleeper_draft_run as loop  # noqa: E402
import wes_execute  # noqa: E402
import wes_sleeper  # noqa: E402

CAND = {"player_key": "77", "name": "Target Guy", "positions": ["RB"],
        "team": "SF", "vor": 12.0}


def _decision(cand, reason="why", source="model", considered=None,
              runner_up=None, why_not=""):
    """A decision dict shaped like wes_draft_agent.decide_one returns.

    Defined once so adding a field to a decision does not mean editing a dozen
    stub lambdas."""
    return {"candidate": cand, "reason": reason, "source": source,
            "considered": considered or [], "runner_up": runner_up,
            "why_not": why_not}


def _state(wait, made=0, cands=(CAND,)):
    """Turn state, as the CHEAP poll returns it. Candidates now come from the
    separate board call — split because polling with the expensive board meant
    each look cost seconds and a fast draft was over before the loop saw its own
    turn (observed live: 65 picks elapsed, 0 taken)."""
    return {"picks_until_turn": wait, "picks_made": made,
            "on_the_clock": wait == 0, "_cands": list(cands)}


def _board(cands=(CAND,)):
    return lambda *_a, **_k: {"candidates": list(cands)}


def _states(seq):
    """Yield each state once, then 'draft over' forever."""
    it = iter(seq)

    def fn(*_a, **_k):
        try:
            return next(it)
        except StopIteration:
            return "That draft is over — all picks are in."
    return fn


class TestRails:
    def test_does_not_pick_when_not_on_the_clock(self, monkeypatch):
        monkeypatch.setattr(wes_execute, "writes_enabled", lambda: True)
        monkeypatch.setattr(wes_sleeper, "drafted_player_ids_fresh",
                            lambda d: set())
        calls = []
        loop.run("D", "L", 1, _state_fn=_states([_state(3), _state(1)]),
                 _board_fn=_board(), _decide_fn=lambda c, **kw: _decision(CAND, "why", "model"),
                 _submit_fn=lambda *a: calls.append(a),
                 _sleep_fn=lambda _s: None)
        assert calls == []

    def test_picks_when_on_the_clock(self, monkeypatch):
        monkeypatch.setattr(wes_execute, "writes_enabled", lambda: True)
        monkeypatch.setattr(wes_sleeper, "drafted_player_ids_fresh",
                            lambda d: set())
        calls = []
        out = loop.run("D", "L", 1, _state_fn=_states([_state(0)]),
                       _board_fn=_board(),
                       _decide_fn=lambda c, **kw: _decision(CAND, "why", "model"),
                       _submit_fn=lambda *a: calls.append(a),
                       _sleep_fn=lambda _s: None)
        assert calls == [("D", "77", "Target Guy")]
        assert "made 1 pick" in out

    def test_never_picks_twice_for_the_same_pick(self, monkeypatch):
        """THE rail. If the API has not caught up, the loop still sees
        picks_made unchanged and would otherwise submit again — a double pick,
        which costs a roster spot and cannot be undone."""
        monkeypatch.setattr(wes_execute, "writes_enabled", lambda: True)
        monkeypatch.setattr(wes_sleeper, "drafted_player_ids_fresh",
                            lambda d: set())
        calls = []
        loop.run("D", "L", 1,
                 # Same pick number three times, as a lagging API would report.
                 _state_fn=_states([_state(0), _state(0), _state(0)]),
                 _board_fn=_board(), _decide_fn=lambda c, **kw: _decision(CAND, "why", "model"),
                 _submit_fn=lambda *a: calls.append(a),
                 _sleep_fn=lambda _s: None)
        assert len(calls) == 1

    def test_it_stops_retrying_once_the_clock_has_moved_on(self, monkeypatch):
        """This used to assert ONE attempt, ever. That was right while a failed
        verification might mean a pick had landed silently -- retrying would
        have risked a double pick.

        It is not right any more, and the old rule cost real picks: the live
        failures are TRANSIENT (a button reading disabled for twenty seconds, a
        commit taking fifteen), and standing down on the first one hands the
        turn to autopick, which then takes every later turn too (2026-08-22).

        Verification now requires the pick's slot to be OURS, so a retry is
        safe. What must still hold is that we stop the moment the clock is not
        ours -- which is what this pins."""
        monkeypatch.setattr(wes_execute, "writes_enabled", lambda: True)
        monkeypatch.setattr(wes_sleeper, "drafted_player_ids_fresh",
                            lambda d: set())
        monkeypatch.setattr(wes_sleeper, "draft_picks", lambda d: [])
        calls = []

        def boom(*a):
            calls.append(a)
            raise RuntimeError("click failed")
        # _state(0) once, then the clock moves on: the retry check sees it is
        # no longer our pick and gives up.
        loop.run("D", "L", 1, _state_fn=_states([_state(0), _state(3),
                                                 _state(3)]),
                 _board_fn=_board(),
                 _decide_fn=lambda c, **kw: _decision(CAND, "why", "model"),
                 _submit_fn=boom, _sleep_fn=lambda _s: None)
        assert len(calls) == 1, "must not keep clicking after our turn passes"

    def test_a_pick_that_landed_late_is_not_clicked_again(self, monkeypatch):
        """The double-pick guard, in its new form: if the first attempt
        actually worked and only the verification was slow, stop."""
        monkeypatch.setattr(wes_execute, "writes_enabled", lambda: True)
        monkeypatch.setattr(wes_sleeper, "drafted_player_ids_fresh",
                            lambda d: set())
        monkeypatch.setattr(wes_sleeper, "draft_picks",
                            lambda d: [{"pick_no": 1, "draft_slot": 1,
                                        "player_id": CAND["player_key"]}])
        calls = []

        def boom(*a):
            calls.append(a)
            raise RuntimeError("verification timed out")
        loop.run("D", "L", 1, _state_fn=_states([_state(0)]),
                 _board_fn=_board(),
                 _decide_fn=lambda c, **kw: _decision(CAND, "why", "model"),
                 _submit_fn=boom, _sleep_fn=lambda _s: None)
        assert len(calls) == 1, "our pick was already there; do not click again"

    def test_rechecks_availability_immediately_before_submitting(self, monkeypatch):
        """The board was true a moment ago; a moment is enough for someone to
        take him — and the re-check must bypass the 15s pick cache, or it is
        reading the same stale list the board was built from. That is exactly
        how a pick was wasted on a kicker who had already been drafted.

        A taken player is now FILTERED OUT of the shortlist rather than
        aborting the pick, so the next-best candidate can still be taken."""
        monkeypatch.setattr(wes_execute, "writes_enabled", lambda: True)
        monkeypatch.setattr(wes_sleeper, "drafted_player_ids_fresh",
                            lambda d: {"77"})       # taken while we thought
        calls = []
        loop.run("D", "L", 1, _state_fn=_states([_state(0)]),
                 _board_fn=_board(), _decide_fn=lambda c, **kw: _decision(CAND, "why", "model"),
                 _submit_fn=lambda *a: calls.append(a),
                 _sleep_fn=lambda _s: None)
        assert calls == []

    def test_writes_off_reports_but_does_not_act(self, monkeypatch):
        monkeypatch.setattr(wes_execute, "writes_enabled", lambda: False)
        monkeypatch.setattr(wes_sleeper, "drafted_player_ids_fresh",
                            lambda d: set())
        calls = []
        loop.run("D", "L", 1, _state_fn=_states([_state(0)]),
                 _board_fn=_board(), _decide_fn=lambda c, **kw: _decision(CAND, "why", "model"),
                 _submit_fn=lambda *a: calls.append(a),
                 _sleep_fn=lambda _s: None)
        assert calls == []

    def test_no_candidates_stands_down_rather_than_looping(self, monkeypatch):
        monkeypatch.setattr(wes_execute, "writes_enabled", lambda: True)
        monkeypatch.setattr(wes_sleeper, "drafted_player_ids_fresh",
                            lambda d: set())
        out = loop.run("D", "L", 1,
                       _state_fn=_states([_state(0), _state(0)]),
                       _board_fn=_board(cands=()),
                       _decide_fn=lambda c, **kw: _decision(CAND, "w", "model"),
                       _submit_fn=lambda *a: None, _sleep_fn=lambda _s: None)
        assert "0 pick" in out

    def test_exits_when_the_draft_is_over(self, monkeypatch):
        monkeypatch.setattr(wes_execute, "writes_enabled", lambda: True)
        out = loop.run("D", "L", 1, _state_fn=lambda *a, **k: "That draft is over.",
                       _sleep_fn=lambda _s: None)
        assert "finished" in out

    def test_a_transient_state_error_does_not_end_the_run(self, monkeypatch):
        """A blip in the API should cost a poll interval, not the draft."""
        monkeypatch.setattr(wes_execute, "writes_enabled", lambda: True)
        monkeypatch.setattr(wes_sleeper, "drafted_player_ids_fresh",
                            lambda d: set())
        calls = []
        loop.run("D", "L", 1,
                 _state_fn=_states(["I couldn't reach Sleeper", _state(0)]),
                 _board_fn=_board(), _decide_fn=lambda c, **kw: _decision(CAND, "why", "model"),
                 _submit_fn=lambda *a: calls.append(a),
                 _sleep_fn=lambda _s: None)
        assert len(calls) == 1

    def test_is_time_bounded(self, monkeypatch):
        """A loop with no deadline is a scheduled task that never exits."""
        monkeypatch.setattr(wes_execute, "writes_enabled", lambda: True)
        t = {"v": 0.0}

        def now():
            t["v"] += 100.0
            return t["v"]
        out = loop.run("D", "L", 1, max_seconds=10,
                       _state_fn=lambda *a, **k: _state(5),
                       _sleep_fn=lambda _s: None, _now_fn=now)
        assert "stopped after" in out


class TestFailureKinds:
    """Two kinds of submit failure, and only one is dangerous (2026-08-15).

    "not in the draft room's available list" happens BEFORE any click, so
    nothing was submitted and the next candidate is safe to try. Treating it
    like an uncertain write forfeited nine picks in a single draft."""

    CANDS = [CAND, {"player_key": "88", "name": "Backup Guy",
                    "positions": ["WR"], "team": "NYJ", "vor": 9.0}]

    def _setup(self, monkeypatch, taken=frozenset()):
        monkeypatch.setattr(wes_execute, "writes_enabled", lambda: True)
        monkeypatch.setattr(wes_sleeper, "drafted_player_ids_fresh",
                            lambda d: set(taken))

    def test_a_pre_click_failure_falls_through_to_the_next_candidate(
            self, monkeypatch):
        self._setup(monkeypatch)
        calls = []

        def submit(draft_id, key, name):
            calls.append(name)
            if key == "77":
                raise RuntimeError(
                    f"{name!r} is not available in the draft room")
            return True
        loop.run("D", "L", 1, _state_fn=_states([_state(0)]),
                 _board_fn=_board(cands=self.CANDS),
                 _decide_fn=lambda c, **kw: _decision(self.CANDS[0], "why"),
                 _submit_fn=submit, _sleep_fn=lambda _s: None)
        assert calls == ["Target Guy", "Backup Guy"]     # tried the next one

    def test_an_UNCERTAIN_failure_still_stands_down(self, monkeypatch):
        """submit_pick already polled ~15s, so retrying is a coin flip on
        whether the first attempt landed late — and that coin flip is a double
        pick."""
        self._setup(monkeypatch)
        calls = []

        def submit(draft_id, key, name):
            calls.append(name)
            raise RuntimeError("clicked but the pick never appeared")
        loop.run("D", "L", 1, _state_fn=_states([_state(0)]),
                 _board_fn=_board(cands=self.CANDS),
                 _decide_fn=lambda c, **kw: _decision(self.CANDS[0], "why"),
                 _submit_fn=submit, _sleep_fn=lambda _s: None)
        assert calls == ["Target Guy"]                   # no second attempt

    def test_players_taken_since_the_board_was_built_are_filtered_out(
            self, monkeypatch):
        """The fresh check removes them from the shortlist, so the model
        chooses among players who actually exist to be drafted."""
        self._setup(monkeypatch, taken={"77"})
        seen = {}

        def decide(cands, **kw):
            seen["keys"] = [c["player_key"] for c in cands]
            return _decision(cands[0], "why")
        loop.run("D", "L", 1, _state_fn=_states([_state(0)]),
                 _board_fn=_board(cands=self.CANDS), _decide_fn=decide,
                 _submit_fn=lambda *a: True, _sleep_fn=lambda _s: None)
        assert seen["keys"] == ["88"]


class TestLedger:
    """Fifteen irreversible decisions deserve a durable, structured record. A
    log file is grep-able at best and gone at worst, and the nine silent
    substitutions that produced four tight ends and no defence were invisible
    precisely because nothing durable said what had been WANTED (2026-08-15)."""

    CAND = {"player_key": "88", "name": "Wanted Guy", "positions": ["RB"],
            "team": "KC", "bye": 10, "vor": 7.5, "market_rank": 12}
    ALT = {"player_key": "99", "name": "Second Choice", "positions": ["WR"],
           "team": "SF", "bye": 6, "vor": 6.0}

    def _rows(self, tmp_path):
        import json
        f = tmp_path / "ledger.jsonl"
        if not f.exists():
            return []
        return [json.loads(x) for x in f.read_text(encoding="utf-8").splitlines()
                if x.strip()]

    def _decision(self, cand=None):
        return {"candidate": cand or self.CAND, "reason": "best back",
                "source": "model", "considered": ["value over replacement 7.5",
                                                  "fills the empty RB slot"],
                "runner_up": "Second Choice", "why_not": "lower value"}

    @pytest.fixture(autouse=True)
    def _no_network(self, monkeypatch):
        """The loop re-checks availability against the live API before every
        pick, deliberately cache-bypassed -- so an unstubbed test goes to the
        network. Via monkeypatch, not assignment: a stub left behind leaks into
        every later test in the session."""
        monkeypatch.setattr(loop.wes_sleeper, "drafted_player_ids_fresh",
                            lambda d: set())

    def _run(self, tmp_path, submit, cands=None):
        loop.run("D", "L", 1, _state_fn=_states([_state(0)]),
                 _board_fn=_board(cands=cands or [self.CAND, self.ALT]),
                 _decide_fn=lambda c, **kw: self._decision(),
                 _submit_fn=submit, _sleep_fn=lambda _s: None,
                 _ledger_path=str(tmp_path / "ledger.jsonl"))
        return self._rows(tmp_path)

    def test_a_successful_pick_is_recorded_with_its_reasoning(self, tmp_path,
                                                              monkeypatch):
        monkeypatch.setattr(loop.wes_execute, "writes_enabled", lambda: True)
        rows = self._run(tmp_path, lambda *a: True)
        assert len(rows) == 1
        r = rows[0]
        assert r["action_type"] == "draft_pick"
        assert r["outcome"] == "drafted" and r["executed"] is True
        assert r["moves"][0]["add"] == "Wanted Guy"
        assert r["why"] == "best back" and r["source"] == "model"
        assert "fills the empty RB slot" in r["considered"]
        assert r["runner_up"] == "Second Choice"

    def test_the_shortlist_is_kept_so_the_pick_is_falsifiable(self, tmp_path,
                                                              monkeypatch):
        """Without the choice set, "took the best available" cannot be
        checked."""
        monkeypatch.setattr(loop.wes_execute, "writes_enabled", lambda: True)
        rows = self._run(tmp_path, lambda *a: True)
        assert rows[0]["shortlist"] == ["Wanted Guy", "Second Choice"]

    def test_a_substitution_records_BOTH_names(self, tmp_path, monkeypatch):
        """The row that was missing. What it wanted and what it got are
        different facts and both matter."""
        monkeypatch.setattr(loop.wes_execute, "writes_enabled", lambda: True)
        calls = []

        def submit(_d, key, name):
            calls.append(name)
            if len(calls) == 1:
                raise RuntimeError("not available in the draft room")
            return True
        rows = self._run(tmp_path, submit)
        r = rows[0]
        assert r["outcome"] == "substituted"
        assert r["moves"][0]["add"] == "Wanted Guy"      # wanted
        assert r["actually_drafted"] == "Second Choice"  # got
        assert "Wanted Guy was gone" in r["note"]

    def test_a_failure_is_recorded_as_not_executed(self, tmp_path, monkeypatch):
        monkeypatch.setattr(loop.wes_execute, "writes_enabled", lambda: True)

        def submit(*_a):
            raise RuntimeError("something uncertain happened")
        rows = self._run(tmp_path, submit, cands=[self.CAND])
        assert rows[0]["outcome"] == "failed"
        assert rows[0]["executed"] is False
        assert "something uncertain" in rows[0]["note"]

    def test_writes_off_still_records_the_decision_as_a_dry_run(self, tmp_path,
                                                               monkeypatch):
        """What it WOULD have done is the whole point of a dry run."""
        monkeypatch.setattr(loop.wes_execute, "writes_enabled", lambda: False)
        rows = self._run(tmp_path, lambda *a: True)
        assert rows[0]["outcome"] == "would_draft"
        assert rows[0]["dry_run"] is True and rows[0]["executed"] is False

    def test_draft_rows_do_not_eat_the_weekly_move_budget(self, tmp_path,
                                                          monkeypatch):
        """A draft is not a waiver move. If these counted, the first roster
        week would open with the cap already blown."""
        monkeypatch.setattr(loop.wes_execute, "writes_enabled", lambda: True)
        self._run(tmp_path, lambda *a: True)
        n = loop.wes_execute.count_recent_moves(
            "sleeper.l.L.r.1", 0, str(tmp_path / "ledger.jsonl"))
        assert n == 0

    def test_the_team_key_is_platform_qualified(self, tmp_path, monkeypatch):
        """One ledger holds both platforms, so a Sleeper roster must never
        collide with a Yahoo team key."""
        monkeypatch.setattr(loop.wes_execute, "writes_enabled", lambda: True)
        rows = self._run(tmp_path, lambda *a: True)
        assert rows[0]["team_key"].startswith("sleeper.")


class TestLedgerIsSandboxedInTests:
    """The first test run after the loop learned to record decisions wrote
    eight fake draft_pick rows into the owner's REAL ledger -- the same shape
    of mistake as the nightly eval writing to the live Yahoo account
    (2026-08-17). conftest sandboxes it autouse; this proves it."""

    def test_the_ledger_path_is_not_the_real_one(self):
        import os
        real = os.path.join(os.path.expanduser("~"), "wes-pc",
                            "fantasy_ledger.jsonl")
        assert os.path.abspath(loop.wes_execute.LEDGER_FILE) != \
            os.path.abspath(real)

    def test_an_unsandboxed_write_lands_in_tmp(self, tmp_path):
        """Belt and braces: a call with no explicit path must still not reach
        the real file."""
        loop.wes_execute.record_action({"ts": 1, "action_type": "probe"})
        assert os.path.exists(loop.wes_execute.LEDGER_FILE)


class TestAutopickGuard:
    """Sleeper turns AUTO-PICK on by itself after ONE missed pick, then takes
    every later turn instantly. So the checks at session build and at pick
    time are both on paths autopick PREVENTS US FROM REACHING -- in the live
    draft of 2026-08-22 it made all nine of our picks and the loop never once
    got to look. The check has to happen while merely waiting."""

    class FakeBrowser:
        def __init__(self, on):
            self.on = on
            self.cleared = 0

        def page(self):
            return "page"

        def peek(self):
            return "page"

        def refresh(self):
            # The guard must RELOAD before reading: a page opened before
            # autopick engaged reports a stale OFF forever.
            self.refreshed = getattr(self, "refreshed", 0) + 1
            return "page"

    def _patch(self, monkeypatch, br):
        monkeypatch.setattr(loop.wes_sleeper, "autopick_on", lambda p: br.on)

        def clear(p, on=False):
            br.cleared += 1
            br.on = on
            return on
        monkeypatch.setattr(loop.wes_sleeper, "set_autopick", clear)

    def test_it_clears_autopick_when_it_finds_it_on(self, monkeypatch):
        br = self.FakeBrowser(on=True)
        self._patch(monkeypatch, br)
        said = []
        loop._keep_autopick_off(br, 0.0, 1000.0, said.append)
        assert br.cleared == 1 and br.on is False
        assert any("switched itself ON" in m for m in said),             "must be loud -- an agent that quietly undoes a platform action "             "cannot be debugged later"

    def test_it_says_nothing_when_all_is_well(self, monkeypatch):
        br = self.FakeBrowser(on=False)
        self._patch(monkeypatch, br)
        said = []
        loop._keep_autopick_off(br, 0.0, 1000.0, said.append)
        assert br.cleared == 0 and said == []

    def test_it_is_rate_limited(self, monkeypatch):
        """One evaluate per cycle would be wasteful; this runs every poll."""
        br = self.FakeBrowser(on=True)
        self._patch(monkeypatch, br)
        t = loop._keep_autopick_off(br, 0.0, 1000.0, lambda m: None)
        loop._keep_autopick_off(br, t, 1000.0 + 5, lambda m: None)
        assert br.cleared == 1, "second call was inside the interval"

    def test_it_checks_again_once_the_interval_passes(self, monkeypatch):
        br = self.FakeBrowser(on=True)
        self._patch(monkeypatch, br)
        t = loop._keep_autopick_off(br, 0.0, 1000.0, lambda m: None)
        br.on = True                      # Sleeper turned it back on
        loop._keep_autopick_off(br, t, 1000.0 + loop.AUTOPICK_CHECK_S + 1,
                                lambda m: None)
        assert br.cleared == 2

    def test_no_browser_is_not_a_crash(self):
        assert loop._keep_autopick_off(None, 0.0, 1000.0, lambda m: None) == 0.0

    def test_a_broken_check_never_breaks_the_draft(self, monkeypatch):
        br = self.FakeBrowser(on=True)

        def boom(_p):
            raise RuntimeError("page gone")
        monkeypatch.setattr(loop.wes_sleeper, "autopick_on", boom)
        said = []
        loop._keep_autopick_off(br, 0.0, 1000.0, said.append)
        assert any("autopick check failed" in m for m in said)


class TestAutopickGuardReadsFreshState:
    def test_it_reloads_before_reading(self, monkeypatch):
        """A check that cannot observe the thing it guards is worse than no
        check, because it reassures. The stale-DOM version reported nothing
        while autopick took all nine of our picks (2026-08-22)."""
        br = TestAutopickGuard.FakeBrowser(on=True)
        monkeypatch.setattr(loop.wes_sleeper, "autopick_on", lambda p: br.on)
        monkeypatch.setattr(loop.wes_sleeper, "set_autopick",
                            lambda p, on=False: setattr(br, "on", on) or on)
        loop._keep_autopick_off(br, 0.0, 1000.0, lambda m: None)
        assert getattr(br, "refreshed", 0) == 1

    def test_nothing_built_yet_means_nothing_to_check(self, monkeypatch):
        class NotBuilt(TestAutopickGuard.FakeBrowser):
            def peek(self):
                return None
        br = NotBuilt(on=True)
        loop._keep_autopick_off(br, 0.0, 1000.0, lambda m: None)
        assert getattr(br, "refreshed", 0) == 0


class TestUnknownIsNotOff:
    """A querySelector on a control that has not rendered returns null, and
    autopick_on() reports None for it. Reading that as False is why the guard
    stayed silent while autopick ran a whole draft (2026-08-22) -- the same
    unknown-versus-zero mistake this project keeps making."""

    def test_an_unreadable_toggle_is_not_treated_as_off(self, monkeypatch):
        br = TestAutopickGuard.FakeBrowser(on=True)
        monkeypatch.setattr(loop.wes_sleeper, "autopick_on", lambda p: None)
        cleared = []
        monkeypatch.setattr(loop.wes_sleeper, "set_autopick",
                            lambda p, on=False: cleared.append(1))
        said = []
        got = loop._keep_autopick_off(br, 0.0, 1000.0, said.append)
        assert cleared == [], "must not act on a state it could not read"
        assert any("UNKNOWN" in m for m in said), "must say so"
        assert got == 0.0, "must retry next cycle, not wait another interval"


class TestLandedLateChecksTheirPlayer:
    """"Our pick number was filled by our slot" is NOT "we got our player".

    Conflating them reported drafting Bijan Robinson while he went to another
    manager at the next pick and our own turn had been taken by autopick with
    somebody else (2026-08-22). It bypassed the slot-scoped verification
    standing right beside it."""

    def test_somebody_elses_player_in_our_slot_is_not_success(self,
                                                              monkeypatch):
        monkeypatch.setattr(wes_execute, "writes_enabled", lambda: True)
        monkeypatch.setattr(wes_sleeper, "drafted_player_ids_fresh",
                            lambda d: set())
        # Our pick number, our slot -- but autopick chose a different player.
        monkeypatch.setattr(wes_sleeper, "draft_picks",
                            lambda d: [{"pick_no": 1, "draft_slot": 1,
                                        "player_id": "SOMEONE-ELSE"}])
        made = []

        def boom(*a):
            made.append(a)
            raise RuntimeError("click did nothing")
        out = loop.run("D", "L", 1, _state_fn=_states([_state(0), _state(3)]),
                       _board_fn=_board(),
                       _decide_fn=lambda c, **kw: _decision(CAND, "why",
                                                            "model"),
                       _submit_fn=boom, _sleep_fn=lambda _s: None)
        assert "made 0 pick(s)" in out,             "must not count another player in our slot as our pick"


class TestBanterKnowsItsOwnRoster:
    """Told "you're the one that took Puka you dumb dumb", banter replied that
    at least its first-rounder was not a questionable gamble -- about its own
    first-round pick, who was listed questionable. It held `our_slot` and
    `draft_slot` and was left to join them itself (2026-08-22).
    """

    PICKS = [
        {"pick_no": 1, "round": 1, "draft_slot": 1, "player_id": "9493",
         "metadata": {"first_name": "Puka", "last_name": "Nacua",
                      "position": "WR"}},
        {"pick_no": 2, "round": 1, "draft_slot": 2, "player_id": "22",
         "metadata": {"first_name": "Bijan", "last_name": "Robinson",
                      "position": "RB"}},
    ]

    @pytest.fixture(autouse=True)
    def _stub(self, monkeypatch):
        monkeypatch.setattr(wes_sleeper, "draft_picks",
                            lambda d, **k: list(self.PICKS))
        monkeypatch.setattr(wes_sleeper, "slot_names",
                            lambda d, **k: {1: "GMBartimusPrime",
                                            2: "awarmwalrus"})

    def _ctx(self, roster_id=1):
        return loop._banter_context("D", "L", roster_id,
                                    {"round": 1, "picks_made": 2}, 10,
                                    lambda *a, **k: None)

    def test_our_pick_is_labelled_ours(self):
        ours = [p for p in self._ctx()["recent_picks"] if p["player"]
                == "Puka Nacua"][0]
        assert ours["ours"] is True and ours["by"] == "US"

    def test_someone_elses_pick_carries_their_name(self):
        theirs = [p for p in self._ctx()["recent_picks"]
                  if p["player"] == "Bijan Robinson"][0]
        assert theirs["ours"] is False and theirs["by"] == "awarmwalrus"

    def test_our_roster_is_its_own_field(self):
        assert self._ctx()["our_roster"] == [
            {"player": "Puka Nacua", "position": "WR", "round": 1}]

    def test_rosters_are_keyed_by_manager_not_slot_number(self):
        assert set(self._ctx()["rosters_by_slot"]) == {"US", "awarmwalrus"}

    def test_ownership_follows_our_actual_slot(self):
        """Sitting in slot 2, Bijan is ours and Puka is not."""
        ctx = self._ctx(roster_id=2)
        by = {p["player"]: p["by"] for p in ctx["recent_picks"]}
        assert by == {"Puka Nacua": "GMBartimusPrime", "Bijan Robinson": "US"}
        assert [r["player"] for r in ctx["our_roster"]] == ["Bijan Robinson"]

    def test_unnameable_managers_do_not_break_the_context(self):
        """A mock seat whose id will not resolve still gets a pick entry."""
        import wes_sleeper as ws
        orig = ws.slot_names
        try:
            ws.slot_names = lambda d, **k: {}
            ctx = self._ctx()
            assert ctx["recent_picks"][1]["by"] is None
            assert ctx["recent_picks"][0]["by"] == "US", "ours needs no lookup"
        finally:
            ws.slot_names = orig
