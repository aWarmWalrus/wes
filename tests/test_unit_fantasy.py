"""Unit tests for the fantasy valuation layer (#029 P1) — network-free.

Covers wes_nba's season-stats parser + name->id resolver (the two things stable
without the live ESPN API, exercised on a saved fixture + injected _get_fn) and
the wes_fantasy engine (formatting + degradation). A live schema-drift check
belongs in a WES_NBA_LIVE-style canary, not here.
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "pc"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import wes_nba as nba  # noqa: E402
import wes_fantasy as fan  # noqa: E402

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures",
                       "espn_athlete_stats.json")
with open(FIXTURE, encoding="utf-8") as _f:
    ATHLETE = json.load(_f)

CATS = ["PTS", "REB", "AST", "ST", "BLK", "TO", "DD", "TD", "EJCT"]


class TestParseSeasonStats:
    def test_parses_latest_season_line(self):
        p = nba.parse_season_stats(ATHLETE, name="Luka Doncic")
        assert p["name"] == "Luka Doncic"
        assert p["season"] == "2025-26"          # newest split, not the first
        assert p["gp"] == 64 and p["min"] == 35.8

    def test_maps_all_league_categories(self):
        cats = nba.parse_season_stats(ATHLETE)["cats"]
        for c in CATS:
            assert c in cats, f"missing {c}"
        assert cats["PTS"] == 33.5 and cats["ST"] == 1.6  # STL -> ST

    def test_counting_cats_are_int_totals(self):
        p = nba.parse_season_stats(ATHLETE)
        assert p["cats"]["DD"] == 34 and isinstance(p["cats"]["DD"], int)
        assert p["counting"] == {"DD", "TD", "EJCT"}

    def test_latest_season_picks_max(self):
        rows = [{"season": {"displayName": "2019-20"}},
                {"season": {"displayName": "2025-26"}},
                {"season": {"displayName": "2023-24"}}]
        assert nba._latest_season(rows) == "2025-26"

    def test_traded_season_takes_max_gp_row(self):
        # a mid-season trade => two rows for one season; take the combined (max GP)
        labels = ["GP", "PTS"]
        rows = [
            {"season": {"displayName": "2025-26"}, "stats": ["20", "10"]},  # team A
            {"season": {"displayName": "2025-26"}, "stats": ["60", "18"]},  # combined
            {"season": {"displayName": "2025-26"}, "stats": ["40", "22"]},  # team B
        ]
        row = nba._row_for(labels, rows, "2025-26")
        assert row["GP"] == "60"

    def test_no_season_returns_none(self):
        assert nba.parse_season_stats({"categories": []}) is None


class TestAthleteId:
    def _search(self, *links):
        return {"results": [{"contents": [{"link": {"web": w}} for w in links]}]}

    def test_extracts_nba_player_id(self):
        data = self._search(
            "https://www.espn.com/nfl/player/_/id/2573402/cam-thomas",
            "https://www.espn.com/nba/player/_/id/4432174/cam-thomas")
        assert nba.athlete_id("Cam Thomas", _get_fn=lambda u: data) == "4432174"

    def test_skips_non_nba_namesakes(self):
        data = self._search(
            "https://www.espn.com/college-football/player/_/id/5154966/cam-thomas")
        assert nba.athlete_id("Cam Thomas", _get_fn=lambda u: data) is None

    def test_degrades_on_error(self):
        def boom(u):
            raise OSError("espn down")
        assert nba.athlete_id("x", _get_fn=boom) is None


class TestPlayerSeasonStats:
    def test_blank_name(self):
        assert "Which player" in nba.player_season_stats("  ")

    def test_unknown_player_degrades(self):
        # search returns no nba link -> no id
        out = nba.player_season_stats("Nobody", _get_fn=lambda u: {"results": []})
        assert isinstance(out, str) and "couldn't find" in out.lower()

    def test_success_returns_dict(self):
        def fake_get(url):
            if "/search/" in url:
                return {"results": [{"contents": [
                    {"link": {"web": "/nba/player/_/id/99/luka"}}]}]}
            return ATHLETE
        out = nba.player_season_stats("Luka", _get_fn=fake_get)
        assert isinstance(out, dict) and out["season"] == "2025-26"


class TestPlayerValue:
    STATS = {"name": "Test Guy", "season": "2025-26", "gp": 70, "min": 30.0,
             "cats": {"PTS": 20.0, "REB": 5.0, "AST": 6.0, "ST": 1.0, "BLK": 0.4,
                      "TO": 2.5, "DD": 10, "TD": 1, "EJCT": 0},
             "counting": {"DD", "TD", "EJCT"}}

    def test_format_shows_all_cats_with_context(self):
        out = fan.format_value(self.STATS, CATS)
        assert out.startswith("Test Guy (2025-26, 70 GP, 30 MPG)")
        assert "PTS 20" in out and "DD 10" in out and "TO 2.5" in out

    def test_missing_cat_renders_dash(self):
        s = dict(self.STATS, cats={"PTS": 20.0})
        assert "REB —" in fan.format_value(s, ["PTS", "REB"])

    def test_versus_compares_two_and_notes_semantics(self):
        seen = []

        def fake(name):
            seen.append(name)
            return dict(self.STATS, name=name)
        out = fan.player_value("A", categories=CATS, versus="B", _stats_fn=fake)
        assert seen == ["A", "B"]
        assert out.count("2025-26") == 2
        assert "lower is better" in out  # TO semantics surfaced

    def test_degradation_passes_through(self):
        out = fan.player_value(
            "X", categories=CATS,
            _stats_fn=lambda n: "I couldn't reach the NBA stats just now.")
        assert out == "I couldn't reach the NBA stats just now."

    def test_default_categories_when_none(self):
        out = fan.player_value("A", _stats_fn=lambda n: self.STATS)
        assert "PTS 20" in out  # falls back to DEFAULT_CATEGORIES
