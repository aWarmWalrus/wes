"""Unit tests for the scheduled Fantasy GM runner (fantasy_gm_run.py, #029).

Network-free: teams and the propose function are both injected.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "pc"))

import fantasy_gm_run as run  # noqa: E402


class TestRunAll:
    def _teams(self, *records):
        return lambda: list(records)

    def test_skips_advise_only_teams(self):
        calls = []
        ok = run.run_all(
            self._teams({"name": "A", "autonomy": "advise"}),
            lambda name: calls.append(name) or "n/a")
        assert calls == []
        assert ok is True

    def test_runs_propose_and_auto_teams(self):
        calls = []
        ok = run.run_all(
            self._teams({"name": "A", "autonomy": "propose"},
                       {"name": "B", "autonomy": "auto"}),
            lambda name: calls.append(name) or "already optimal")
        assert calls == ["A", "B"]
        assert ok is True

    def test_autonomy_is_case_insensitive(self):
        calls = []
        run.run_all(self._teams({"name": "A", "autonomy": "AUTO"}),
                    lambda name: calls.append(name) or "ok")
        assert calls == ["A"]

    def test_no_configured_teams_is_not_an_error(self):
        assert run.run_all(self._teams(), lambda name: "x") is True

    def test_one_teams_exception_does_not_stop_the_rest(self):
        calls = []

        def propose(name):
            if name == "A":
                raise RuntimeError("espn down")
            calls.append(name)
            return "already optimal"
        ok = run.run_all(
            self._teams({"name": "A", "autonomy": "auto"},
                       {"name": "B", "autonomy": "auto"}),
            propose)
        assert calls == ["B"]          # B still ran despite A's crash
        assert ok is False             # but the run is flagged as having erred

    def test_all_teams_succeeding_is_ok_even_with_no_moves(self):
        ok = run.run_all(
            self._teams({"name": "A", "autonomy": "propose"}),
            lambda name: "No lineup changes needed for A — already optimal.")
        assert ok is True

    def test_a_real_execution_does_not_count_as_an_error(self):
        """An actual write succeeding is a NORMAL outcome, not a run failure —
        only an exception should flip `ok` to False."""
        ok = run.run_all(
            self._teams({"name": "A", "autonomy": "auto"}),
            lambda name: "Set the lineup for A:\n  X: BN -> QB")
        assert ok is True

    def test_missing_name_field_degrades_to_a_placeholder(self):
        """A malformed team record must not crash the whole cycle."""
        ok = run.run_all(self._teams({"autonomy": "auto"}),
                         lambda name: "ok")
        assert ok is True
