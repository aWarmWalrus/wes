"""Unit tests for the guarded snapshot refresh (snapshot_refresh, #040).

The point of this module is the ROLLBACK, so that is what is tested: a thin
snapshot is the dangerous outcome, because it looks exactly like a healthy one
to anything that checks age -- which is all the pre-flight checks.
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "pc"))

import snapshot_refresh as sr  # noqa: E402

GOOD = {"counts": {"projections": 324, "players": 12221, "byes": 32}}
THIN = {"counts": {"projections": 3, "players": 12221, "byes": 32}}


def _existing(tmp_path, body="old"):
    p = tmp_path / "snap.json"
    p.write_text(json.dumps({"marker": body}), encoding="utf-8")
    return str(p)


class TestRefresh:
    def test_a_healthy_build_is_installed(self, tmp_path):
        path = _existing(tmp_path)

        def build(path=None):
            open(path, "w", encoding="utf-8").write('{"marker": "new"}')
            return GOOD
        ok, msg = sr.refresh(path=path, _build_fn=build, _log=lambda *a: None)
        assert ok, msg
        assert json.load(open(path, encoding="utf-8"))["marker"] == "new"

    def test_a_THIN_build_is_rejected_and_rolled_back(self, tmp_path):
        """The dangerous case: it writes cleanly, and every freshness check
        in the system would call it current."""
        path = _existing(tmp_path)

        def build(path=None):
            open(path, "w", encoding="utf-8").write('{"marker": "thin"}')
            return THIN
        ok, msg = sr.refresh(path=path, _build_fn=build, _log=lambda *a: None)
        assert not ok and "REJECTED" in msg and "projections 3" in msg
        assert json.load(open(path, encoding="utf-8"))["marker"] == "old"

    def test_a_RAISING_build_keeps_the_old_snapshot(self, tmp_path):
        path = _existing(tmp_path)

        def build(path=None):
            open(path, "w", encoding="utf-8").write("half-written garbage")
            raise RuntimeError("ESPN said no")
        ok, msg = sr.refresh(path=path, _build_fn=build, _log=lambda *a: None)
        assert not ok and "kept the old one" in msg
        assert json.load(open(path, encoding="utf-8"))["marker"] == "old"

    def test_too_few_players_is_also_rejected(self, tmp_path):
        path = _existing(tmp_path)
        ok, msg = sr.refresh(
            path=path, _log=lambda *a: None,
            _build_fn=lambda path=None: {"counts": {"projections": 324,
                                                    "players": 12, "byes": 32}})
        assert not ok and "players 12" in msg

    def test_missing_byes_is_also_rejected(self, tmp_path):
        path = _existing(tmp_path)
        ok, msg = sr.refresh(
            path=path, _log=lambda *a: None,
            _build_fn=lambda path=None: {"counts": {"projections": 324,
                                                    "players": 12221,
                                                    "byes": 0}})
        assert not ok and "byes 0" in msg

    def test_a_thin_FIRST_build_says_there_is_nothing_to_fall_back_to(
            self, tmp_path):
        """Rolling back to a snapshot that does not exist would be worse than
        saying so."""
        path = str(tmp_path / "none.json")
        ok, msg = sr.refresh(path=path, _log=lambda *a: None,
                             _build_fn=lambda path=None: THIN)
        assert not ok and "no previous snapshot" in msg

    def test_the_threshold_is_tunable(self, tmp_path):
        path = _existing(tmp_path)
        ok, _msg = sr.refresh(path=path, min_projections=1,
                              _log=lambda *a: None,
                              _build_fn=lambda path=None: THIN)
        assert ok
