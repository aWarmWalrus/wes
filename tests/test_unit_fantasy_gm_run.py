"""Unit tests for the scheduled Fantasy GM runner (fantasy_gm_run.py, #029/#035).

Network-free: teams, the lineup check and the roster check are all injected.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "pc"))

import fantasy_gm_run as run  # noqa: E402

# Every run_all() call here MUST inject BOTH checks. The roster check defaults
# to the real, NETWORKED propose_roster_moves, so a test that injects only the
# lineup one silently launches Playwright and hits ESPN — which is exactly what
# happened when the roster check was first added (real Chrome processes, a hung
# suite). NOOP is the guard against that regressing.
NOOP = lambda name: "already optimal"  # noqa: E731


class TestRunAll:
    def _teams(self, *records):
        return lambda: list(records)

    def test_skips_advise_only_teams(self):
        calls = []
        ok = run.run_all(self._teams({"name": "A", "autonomy": "advise"}),
                         lambda name: calls.append(name) or "n/a", NOOP)
        assert calls == []
        assert ok is True

    def test_runs_propose_and_auto_teams(self):
        calls = []
        ok = run.run_all(
            self._teams({"name": "A", "autonomy": "propose"},
                        {"name": "B", "autonomy": "auto"}),
            lambda name: calls.append(name) or "already optimal", NOOP)
        assert calls == ["A", "B"]
        assert ok is True

    def test_roster_runs_BEFORE_lineup(self):
        """#035, order corrected 2026-08-14. Both checks run, and the ORDER is
        load-bearing: a pickup lands on the BENCH, so optimizing the lineup
        first leaves the player just acquired -- picked because he beats someone
        already rostered -- benched until the next run, up to 24h later and
        possibly through kickoff. The move gets made and then wasted."""
        seen = []
        run.run_all(self._teams({"name": "A", "autonomy": "auto"}),
                    lambda name: seen.append("lineup") or "ok",
                    lambda name: seen.append("roster") or "ok")
        assert seen == ["roster", "lineup"]

    def test_autonomy_is_case_insensitive(self):
        calls = []
        run.run_all(self._teams({"name": "A", "autonomy": "AUTO"}),
                    lambda name: calls.append(name) or "ok", NOOP)
        assert calls == ["A"]

    def test_no_configured_teams_is_not_an_error(self):
        assert run.run_all(self._teams(), lambda name: "x", NOOP) is True

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
            propose, NOOP)
        assert calls == ["B"]          # B still ran despite A's crash
        assert ok is False             # but the run is flagged as having erred

    def test_a_failing_lineup_check_does_not_skip_the_roster_check(self):
        """One check crashing must not cost the other — they're independent
        questions about the same team."""
        seen = []

        def boom(name):
            raise RuntimeError("lineup blew up")
        ok = run.run_all(self._teams({"name": "A", "autonomy": "auto"}),
                         boom, lambda name: seen.append("roster") or "ok")
        assert seen == ["roster"]
        assert ok is False

    def test_a_failing_roster_check_does_not_skip_the_lineup_check(self):
        """The mirror case, and the reason putting the IRREVERSIBLE step first
        costs nothing: if the roster pass blows up, the lineup still gets set."""
        seen = []

        def boom(name):
            raise RuntimeError("roster blew up")
        ok = run.run_all(self._teams({"name": "A", "autonomy": "auto"}),
                         lambda name: seen.append("lineup") or "ok", boom)
        assert seen == ["lineup"]
        assert ok is False

    def test_all_teams_succeeding_is_ok_even_with_no_moves(self):
        ok = run.run_all(
            self._teams({"name": "A", "autonomy": "propose"}),
            lambda name: "No lineup changes needed for A — already optimal.",
            NOOP)
        assert ok is True

    def test_a_real_execution_does_not_count_as_an_error(self):
        """An actual write succeeding is a NORMAL outcome, not a run failure —
        only an exception should flip `ok` to False."""
        ok = run.run_all(self._teams({"name": "A", "autonomy": "auto"}),
                         lambda name: "Set the lineup for A:\n  X: BN -> QB",
                         NOOP)
        assert ok is True

    def test_missing_name_field_degrades_to_a_placeholder(self):
        """A malformed team record must not crash the whole cycle."""
        ok = run.run_all(self._teams({"autonomy": "auto"}),
                         lambda name: "ok", NOOP)
        assert ok is True
