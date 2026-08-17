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

    def test_a_failed_submit_is_NOT_retried(self, monkeypatch):
        """submit_pick already polled ~15s before giving up, so a retry is a
        coin flip on whether the first landed late — and that coin flip is a
        double pick. cpu_autopick covers the miss."""
        monkeypatch.setattr(wes_execute, "writes_enabled", lambda: True)
        monkeypatch.setattr(wes_sleeper, "drafted_player_ids_fresh",
                            lambda d: set())
        calls = []

        def boom(*a):
            calls.append(a)
            raise RuntimeError("click failed")
        loop.run("D", "L", 1, _state_fn=_states([_state(0), _state(0)]),
                 _board_fn=_board(), _decide_fn=lambda c, **kw: _decision(CAND, "why", "model"),
                 _submit_fn=boom, _sleep_fn=lambda _s: None)
        assert len(calls) == 1

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
