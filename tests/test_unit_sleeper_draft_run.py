"""Unit tests for the draft loop (sleeper_draft_run, #039).

Network-free: state, decision, submit and sleep are all injected, so what is
tested is the RAILS — which is the part that can lose you a roster spot.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "pc"))

import sleeper_draft_run as loop  # noqa: E402
import wes_execute  # noqa: E402
import wes_sleeper  # noqa: E402

CAND = {"player_key": "77", "name": "Target Guy", "positions": ["RB"],
        "team": "SF", "vor": 12.0}


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
        monkeypatch.setattr(wes_sleeper, "drafted_player_ids", lambda d: set())
        calls = []
        loop.run("D", "L", 1, _state_fn=_states([_state(3), _state(1)]),
                 _board_fn=_board(), _decide_fn=lambda c, **kw: (CAND, "why", "model"),
                 _submit_fn=lambda *a: calls.append(a),
                 _sleep_fn=lambda _s: None)
        assert calls == []

    def test_picks_when_on_the_clock(self, monkeypatch):
        monkeypatch.setattr(wes_execute, "writes_enabled", lambda: True)
        monkeypatch.setattr(wes_sleeper, "drafted_player_ids", lambda d: set())
        calls = []
        out = loop.run("D", "L", 1, _state_fn=_states([_state(0)]),
                       _board_fn=_board(),
                       _decide_fn=lambda c, **kw: (CAND, "why", "model"),
                       _submit_fn=lambda *a: calls.append(a),
                       _sleep_fn=lambda _s: None)
        assert calls == [("D", "77", "Target Guy")]
        assert "made 1 pick" in out

    def test_never_picks_twice_for_the_same_pick(self, monkeypatch):
        """THE rail. If the API has not caught up, the loop still sees
        picks_made unchanged and would otherwise submit again — a double pick,
        which costs a roster spot and cannot be undone."""
        monkeypatch.setattr(wes_execute, "writes_enabled", lambda: True)
        monkeypatch.setattr(wes_sleeper, "drafted_player_ids", lambda d: set())
        calls = []
        loop.run("D", "L", 1,
                 # Same pick number three times, as a lagging API would report.
                 _state_fn=_states([_state(0), _state(0), _state(0)]),
                 _board_fn=_board(), _decide_fn=lambda c, **kw: (CAND, "why", "model"),
                 _submit_fn=lambda *a: calls.append(a),
                 _sleep_fn=lambda _s: None)
        assert len(calls) == 1

    def test_a_failed_submit_is_NOT_retried(self, monkeypatch):
        """submit_pick already polled ~15s before giving up, so a retry is a
        coin flip on whether the first landed late — and that coin flip is a
        double pick. cpu_autopick covers the miss."""
        monkeypatch.setattr(wes_execute, "writes_enabled", lambda: True)
        monkeypatch.setattr(wes_sleeper, "drafted_player_ids", lambda d: set())
        calls = []

        def boom(*a):
            calls.append(a)
            raise RuntimeError("click failed")
        loop.run("D", "L", 1, _state_fn=_states([_state(0), _state(0)]),
                 _board_fn=_board(), _decide_fn=lambda c, **kw: (CAND, "why", "model"),
                 _submit_fn=boom, _sleep_fn=lambda _s: None)
        assert len(calls) == 1

    def test_rechecks_availability_immediately_before_submitting(self, monkeypatch):
        """The board was true a moment ago; a moment is enough for someone to
        take him. Clicking a row that no longer means what we think is how the
        wrong player gets drafted."""
        monkeypatch.setattr(wes_execute, "writes_enabled", lambda: True)
        monkeypatch.setattr(wes_sleeper, "drafted_player_ids",
                            lambda d: {"77"})       # taken while we thought
        calls = []
        loop.run("D", "L", 1, _state_fn=_states([_state(0)]),
                 _board_fn=_board(), _decide_fn=lambda c, **kw: (CAND, "why", "model"),
                 _submit_fn=lambda *a: calls.append(a),
                 _sleep_fn=lambda _s: None)
        assert calls == []

    def test_writes_off_reports_but_does_not_act(self, monkeypatch):
        monkeypatch.setattr(wes_execute, "writes_enabled", lambda: False)
        monkeypatch.setattr(wes_sleeper, "drafted_player_ids", lambda d: set())
        calls = []
        loop.run("D", "L", 1, _state_fn=_states([_state(0)]),
                 _board_fn=_board(), _decide_fn=lambda c, **kw: (CAND, "why", "model"),
                 _submit_fn=lambda *a: calls.append(a),
                 _sleep_fn=lambda _s: None)
        assert calls == []

    def test_no_candidates_stands_down_rather_than_looping(self, monkeypatch):
        monkeypatch.setattr(wes_execute, "writes_enabled", lambda: True)
        monkeypatch.setattr(wes_sleeper, "drafted_player_ids", lambda d: set())
        out = loop.run("D", "L", 1,
                       _state_fn=_states([_state(0), _state(0)]),
                       _board_fn=_board(cands=()),
                       _decide_fn=lambda c, **kw: (CAND, "w", "model"),
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
        monkeypatch.setattr(wes_sleeper, "drafted_player_ids", lambda d: set())
        calls = []
        loop.run("D", "L", 1,
                 _state_fn=_states(["I couldn't reach Sleeper", _state(0)]),
                 _board_fn=_board(), _decide_fn=lambda c, **kw: (CAND, "why", "model"),
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
