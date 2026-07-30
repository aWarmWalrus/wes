"""Unit tests for the gated executor + action ledger (wes_execute, #029 P3).

STATUS: shadow-mode only — no test here exercises a real Yahoo write, because
none exists yet (see wes_execute.py's module docstring). These tests pin the
guardrail logic, the diff, and the ledger, all of which are real and load-
bearing before any write is built.
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "pc"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import wes_execute as ex  # noqa: E402


class TestDiffLineup:
    def _p(self, name, slot, key="k1"):
        return {"name": name, "slot": slot, "player_key": key}

    def test_a_starter_who_should_move_is_a_move(self):
        players = [self._p("A", "BN", "1")]
        result = {"starters": [{"slot": "QB", "name": "A", "value": 10}],
                  "bench": []}
        moves = ex.diff_lineup(players, result)
        assert moves == [{"player_key": "1", "name": "A",
                          "from_slot": "BN", "to_slot": "QB"}]

    def test_a_player_already_in_the_right_slot_is_not_a_move(self):
        players = [self._p("A", "QB", "1")]
        result = {"starters": [{"slot": "QB", "name": "A", "value": 10}],
                  "bench": []}
        assert ex.diff_lineup(players, result) == []

    def test_a_bench_recommendation_for_a_current_starter_is_a_move(self):
        players = [self._p("A", "QB", "1")]
        result = {"starters": [], "bench": ["A"]}
        moves = ex.diff_lineup(players, result)
        assert moves == [{"player_key": "1", "name": "A",
                          "from_slot": "QB", "to_slot": "BN"}]

    @pytest.mark.parametrize("alias", ["IR", "IL", "IL+", "BE", "NA"])
    def test_bench_aliases_collapse_so_ir_to_bn_is_not_a_spurious_move(self, alias):
        """Yahoo has several non-starting slots; recommending BN for someone
        already on IR isn't a real move."""
        players = [self._p("A", alias, "1")]
        result = {"starters": [], "bench": ["A"]}
        assert ex.diff_lineup(players, result) == []

    def test_a_player_not_mentioned_by_the_optimizer_is_untouched(self):
        """Only players the RESULT talks about can generate a move — a player
        missing from both starters and bench must not be silently benched."""
        players = [self._p("A", "QB", "1"), self._p("Ghost", "WR", "2")]
        result = {"starters": [{"slot": "QB", "name": "A", "value": 10}],
                  "bench": []}
        assert ex.diff_lineup(players, result) == []

    def test_no_moves_when_lineup_already_matches(self):
        players = [self._p("A", "QB", "1"), self._p("B", "BN", "2")]
        result = {"starters": [{"slot": "QB", "name": "A", "value": 10}],
                  "bench": ["B"]}
        assert ex.diff_lineup(players, result) == []


class TestGuardrails:
    def test_advise_teams_refuse_everything(self):
        allowed, reason = ex.check_guardrails({"autonomy": "advise"}, "set_lineup")
        assert allowed is False
        assert "advise-only" in reason

    def test_action_not_in_allowlist_is_refused(self):
        team = {"autonomy": "propose", "guardrails": {"actions_allowed": []}}
        allowed, reason = ex.check_guardrails(team, "set_lineup")
        assert allowed is False and "actions_allowed" in reason

    def test_allowed_action_under_propose_passes(self):
        team = {"autonomy": "propose",
                "guardrails": {"actions_allowed": ["set_lineup"]}}
        allowed, reason = ex.check_guardrails(team, "set_lineup")
        assert allowed is True and reason == ""

    def test_allowed_action_under_auto_passes(self):
        team = {"autonomy": "auto",
                "guardrails": {"actions_allowed": ["set_lineup", "waiver_claim"]}}
        assert ex.check_guardrails(team, "set_lineup") == (True, "")

    def test_missing_autonomy_defaults_to_advise_and_refuses(self):
        allowed, reason = ex.check_guardrails({}, "set_lineup")
        assert allowed is False

    def test_stale_data_is_refused_per_freshness_guardrail(self):
        team = {"autonomy": "auto",
                "guardrails": {"actions_allowed": ["set_lineup"],
                               "require_fresh_data_minutes": 90},
                "_data_fetched_at": 0.0}
        allowed, reason = ex.check_guardrails(team, "set_lineup", _now=6000.0)
        assert allowed is False and "stale" in reason.lower()

    def test_fresh_data_passes_the_freshness_guardrail(self):
        team = {"autonomy": "auto",
                "guardrails": {"actions_allowed": ["set_lineup"],
                               "require_fresh_data_minutes": 90},
                "_data_fetched_at": 1000.0}
        assert ex.check_guardrails(team, "set_lineup", _now=1060.0) == (True, "")

    def test_no_freshness_requirement_means_no_freshness_check(self):
        team = {"autonomy": "auto",
                "guardrails": {"actions_allowed": ["set_lineup"]}}
        assert ex.check_guardrails(team, "set_lineup", _now=99999.0) == (True, "")


class TestLedger:
    def test_append_writes_one_json_line(self, tmp_path):
        path = str(tmp_path / "ledger.jsonl")
        ex._append_ledger({"a": 1}, path)
        ex._append_ledger({"a": 2}, path)
        lines = [json.loads(l) for l in open(path, encoding="utf-8")]
        assert lines == [{"a": 1}, {"a": 2}]

    def test_creates_the_parent_directory(self, tmp_path):
        path = str(tmp_path / "nested" / "dir" / "ledger.jsonl")
        ex._append_ledger({"a": 1}, path)
        assert os.path.exists(path)

    def test_write_failure_does_not_raise(self, monkeypatch):
        """The ledger is observability, not correctness — a failure to write it
        must never crash the turn that triggered it."""
        def boom(*a, **k):
            raise OSError("disk full")
        monkeypatch.setattr(ex.os, "makedirs", boom)
        ex._append_ledger({"a": 1}, "/should/not/matter")   # must not raise

    def test_default_ledger_path_is_pc_local_not_the_repo(self):
        assert "wes-pc" in ex.LEDGER_FILE
        assert "wes-pc" not in os.path.dirname(os.path.abspath(__file__))


class TestLiveWritesKillSwitch:
    def test_default_is_off(self, monkeypatch):
        monkeypatch.delenv("WES_YAHOO_LIVE_WRITES", raising=False)
        import importlib
        importlib.reload(ex)
        assert ex.LIVE_WRITES is False

    def test_submit_lineup_is_not_implemented(self):
        """The placeholder RAISES rather than no-ops, so a caller that forgets
        to check LIVE_WRITES fails loudly instead of believing a write happened."""
        with pytest.raises(NotImplementedError):
            ex._submit_lineup("k", [])


class TestProposeLineupChange:
    RESULT = {"starters": [{"slot": "QB", "name": "Hurts", "value": 19.3}],
             "bench": ["Chase"]}
    PLAYERS = [{"name": "Hurts", "slot": "BN", "player_key": "1"},
              {"name": "Chase", "slot": "WR", "player_key": "2"}]

    def _compute(self, team_key="nfl.l.1.t.1", name="Test", sport="nfl"):
        return lambda team=None: {
            "team_key": team_key, "name": name, "sport": sport,
            "result": self.RESULT, "warn": "", "players": self.PLAYERS}

    def _team(self, monkeypatch, autonomy="propose", allowed=("set_lineup",)):
        monkeypatch.setattr(
            ex.wes_yahoo, "_resolve_team",
            lambda t=None: ({"team_key": "nfl.l.1.t.1", "name": "Test",
                             "autonomy": autonomy,
                             "guardrails": {"actions_allowed": list(allowed)}},
                            None))

    def test_compute_degradation_is_relayed_verbatim(self, monkeypatch):
        self._team(monkeypatch)
        out = ex.propose_lineup_change(
            _compute_fn=lambda team=None: "couldn't reach Yahoo",
            _ledger_path="/dev/null" if os.name != "nt" else None)
        assert out == "couldn't reach Yahoo"

    def test_advise_team_is_told_to_use_the_other_tool(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            ex.wes_yahoo, "_resolve_team",
            lambda t=None: ({"team_key": "x", "name": "Test",
                             "autonomy": "advise"}, None))
        out = ex.propose_lineup_change(
            _compute_fn=self._compute(),
            _ledger_path=str(tmp_path / "l.jsonl"))
        assert "advise-only" in out

    def test_propose_team_gets_a_dry_run_report_naming_the_moves(self, monkeypatch, tmp_path):
        self._team(monkeypatch, autonomy="propose")
        path = tmp_path / "l.jsonl"
        out = ex.propose_lineup_change(_compute_fn=self._compute(),
                                       _ledger_path=str(path))
        assert "Hurts" in out and "Chase" in out
        assert "shadow run" in out.lower()
        logged = json.loads(path.read_text().strip().splitlines()[-1])
        assert logged["dry_run"] is True and logged["executed"] is False
        assert len(logged["moves"]) == 2

    def test_auto_team_without_live_writes_is_a_shadow_run(self, monkeypatch, tmp_path):
        monkeypatch.setattr(ex, "LIVE_WRITES", False)
        self._team(monkeypatch, autonomy="auto")
        out = ex.propose_lineup_change(_compute_fn=self._compute(),
                                       _ledger_path=str(tmp_path / "l.jsonl"))
        assert "auto mode" in out and "live writes are off" in out

    def test_auto_team_with_live_writes_but_no_submit_fn_still_does_not_execute(
            self, monkeypatch, tmp_path):
        """LIVE_WRITES=1 alone must not be enough — _submit_lineup itself raises
        NotImplementedError, so this stays inert until a real write function
        exists, matching the module's documented status."""
        monkeypatch.setattr(ex, "LIVE_WRITES", True)
        self._team(monkeypatch, autonomy="auto")
        out = ex.propose_lineup_change(_compute_fn=self._compute(),
                                       _ledger_path=str(tmp_path / "l.jsonl"))
        assert "not available" in out or "aren't built yet" in out

    def test_auto_team_with_live_writes_and_a_working_submit_fn_executes(
            self, monkeypatch, tmp_path):
        """The one path where a write WOULD actually happen — proven with an
        injected fake, since no real submit function exists to call for real."""
        monkeypatch.setattr(ex, "LIVE_WRITES", True)
        self._team(monkeypatch, autonomy="auto")
        submitted = []
        path = tmp_path / "l.jsonl"
        out = ex.propose_lineup_change(
            _compute_fn=self._compute(),
            _submit_fn=lambda team_key, moves: submitted.append((team_key, moves)),
            _ledger_path=str(path))
        assert "Set the lineup" in out
        assert len(submitted) == 1 and submitted[0][0] == "nfl.l.1.t.1"
        logged = json.loads(path.read_text().strip().splitlines()[-1])
        assert logged["executed"] is True and logged["dry_run"] is False

    def test_blocked_action_is_logged_and_never_reaches_submit(self, monkeypatch, tmp_path):
        monkeypatch.setattr(ex, "LIVE_WRITES", True)
        self._team(monkeypatch, autonomy="auto", allowed=())   # not allowed
        submitted = []
        path = tmp_path / "l.jsonl"
        out = ex.propose_lineup_change(
            _compute_fn=self._compute(),
            _submit_fn=lambda team_key, moves: submitted.append(1),
            _ledger_path=str(path))
        assert submitted == []
        assert "Not proposing" in out
        logged = json.loads(path.read_text().strip().splitlines()[-1])
        assert logged["allowed"] is False

    def test_no_moves_needed_is_reported_and_logged_without_moves(self, monkeypatch, tmp_path):
        self._team(monkeypatch, autonomy="propose")
        compute = lambda team=None: {   # noqa: E731
            "team_key": "nfl.l.1.t.1", "name": "Test", "sport": "nfl",
            "result": {"starters": [{"slot": "QB", "name": "Hurts", "value": 1}],
                      "bench": []},
            "warn": "",
            "players": [{"name": "Hurts", "slot": "QB", "player_key": "1"}]}
        path = tmp_path / "l.jsonl"
        out = ex.propose_lineup_change(_compute_fn=compute, _ledger_path=str(path))
        assert "already optimal" in out
        logged = json.loads(path.read_text().strip().splitlines()[-1])
        assert logged["moves"] == []

    def test_no_team_configured(self, monkeypatch, tmp_path):
        monkeypatch.setattr(ex.wes_yahoo, "_resolve_team",
                           lambda t=None: (None, None))
        out = ex.propose_lineup_change(
            _compute_fn=self._compute(), _ledger_path=str(tmp_path / "l.jsonl"))
        assert "configured" in out
