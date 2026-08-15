"""Unit tests for the draft-board snapshot (wes_snapshot, #039).

Network-free: every fetcher is injected. What is tested is the properties a
SNAPSHOT is supposed to have — reproducibility, honest absence, honest
staleness — not the fetching.
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "pc"))

import wes_snapshot as snap  # noqa: E402

PLAYERS = {"1": {"name": "A", "positions": ["RB"], "team": "SF"}}
PROJ = [{"name": "A", "espn_id": "1", "cats": {"RushYds": 1000.0}}]
BYES = {"SF": 8}


def _build(tmp_path, **over):
    kw = dict(_players_fn=lambda: PLAYERS, _proj_fn=lambda s: PROJ,
              _byes_fn=lambda s: BYES, path=str(tmp_path / "snap.json"))
    kw.update(over)
    return snap.build(season=2026, **kw)


class TestBuild:
    def test_writes_everything_a_board_needs(self, tmp_path):
        s = _build(tmp_path)
        assert s["players"] == PLAYERS
        assert s["projections"] == PROJ
        assert s["byes"] == BYES
        assert s["counts"] == {"players": 1, "projections": 1, "byes": 1}

    def test_the_write_is_atomic(self, tmp_path):
        """A half-written snapshot found at draft time is worse than none: the
        failure arrives as garbled data rather than an honest absence."""
        target = tmp_path / "snap.json"
        _build(tmp_path)
        first = target.read_text(encoding="utf-8")

        def boom(_s):
            raise OSError("fetch died mid-build")
        try:
            _build(tmp_path, _proj_fn=boom)
        except OSError:
            pass
        # The previous good snapshot survives untouched.
        assert target.read_text(encoding="utf-8") == first
        assert not [p for p in os.listdir(tmp_path) if p.endswith(".tmp")]

    def test_records_when_it_was_taken(self, tmp_path):
        s = _build(tmp_path, _now=1000.0)
        assert s["created_at"] == 1000.0


class TestLoad:
    def test_round_trips(self, tmp_path):
        _build(tmp_path)
        got = snap.load(path=str(tmp_path / "snap.json"))
        assert got["players"] == PLAYERS

    def test_a_missing_snapshot_is_None_not_empty(self, tmp_path):
        """Absent and empty are different facts. Returning {} would make the
        board silently value nobody rather than fall back to a live fetch."""
        assert snap.load(path=str(tmp_path / "nope.json")) is None

    def test_a_corrupt_snapshot_reads_as_absent(self, tmp_path):
        p = tmp_path / "bad.json"
        p.write_text("{ not json", encoding="utf-8")
        assert snap.load(path=str(p)) is None

    def test_a_rebuild_is_picked_up_without_a_restart(self, tmp_path):
        """The loop runs for hours; a snapshot rebuilt underneath it should take
        effect, so the in-process cache keys on file mtime rather than time."""
        path = str(tmp_path / "snap.json")
        _build(tmp_path)
        assert snap.load(path=path)["counts"]["players"] == 1
        time.sleep(0.01)
        _build(tmp_path, _players_fn=lambda: {"1": {}, "2": {}})
        assert snap.load(path=path)["counts"]["players"] == 2


class TestStaleness:
    def test_age_is_reported(self, tmp_path):
        s = _build(tmp_path, _now=1000.0)
        assert snap.age_seconds(s, _now=1000.0 + 3600) == 3600

    def test_describe_flags_a_stale_snapshot_loudly(self, tmp_path):
        s = _build(tmp_path, _now=time.time() - (snap.STALE_AFTER_S + 60))
        assert "STALE" in snap.describe(s)

    def test_describe_says_what_to_do_when_there_is_none(self):
        text = snap.describe({})
        assert "No snapshot" in text and "build" in text

    def test_staleness_is_REPORTED_not_auto_repaired(self, tmp_path):
        """A surprise refresh would give back exactly the reproducibility the
        snapshot exists to provide — 'the board I inspected' and 'the board that
        drafted' must be the same artifact."""
        old = _build(tmp_path, _now=1.0)
        loaded = snap.load(path=str(tmp_path / "snap.json"))
        assert loaded["created_at"] == 1.0     # not silently rebuilt


class TestFallback:
    def test_players_falls_back_to_a_live_fetch(self, monkeypatch):
        """A missing snapshot should cost latency, not the draft."""
        monkeypatch.setattr(snap, "load", lambda *a, **k: None)
        assert snap.players(_fallback_fn=lambda: {"x": 1}) == {"x": 1}

    def test_projections_fall_back(self, monkeypatch):
        monkeypatch.setattr(snap, "load", lambda *a, **k: None)
        assert snap.projections(_fallback_fn=lambda: [{"n": 1}]) == [{"n": 1}]

    def test_an_empty_section_falls_back_rather_than_serving_nothing(
            self, monkeypatch):
        """A snapshot built during an ESPN outage could hold zero projections.
        Serving that would value nobody; falling back at least tries."""
        monkeypatch.setattr(snap, "load", lambda *a, **k: {"projections": []})
        assert snap.projections(_fallback_fn=lambda: [{"n": 1}]) == [{"n": 1}]
