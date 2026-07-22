"""Unit tests for the fantasy draft engine (pc/wes_draft.py, ticket #030).

Pure logic tested from fixtures (no network): the ESPN bulk-stats parser, the
positional-need pick recommender, and the end-to-end recommend_pick with an
injected pool. One opt-in live canary (WES_NBA_LIVE=1) hits ESPN's byathlete
endpoint to catch schema drift, like the NBA one.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "pc"))

import pytest  # noqa: E402

import wes_draft as draft  # noqa: E402
import wes_fantasy as fan  # noqa: E402


# A byathlete payload shaped like ESPN's real one, trimmed to the labels the
# parser reads. `names` and `values` are positionally aligned per group — the
# parser zips them, so a subset is fine as long as each pair lines up.
_FIXTURE = {
    "requestedSeason": {"displayName": "2025-26"},
    "categories": [
        {"name": "general",
         "names": ["gamesPlayed", "avgMinutes", "avgRebounds",
                   "doubleDouble", "tripleDouble", "ejections"]},
        {"name": "offensive",
         "names": ["avgPoints", "avgAssists", "avgTurnovers", "fieldGoalPct"]},
        {"name": "defensive", "names": ["avgSteals", "avgBlocks"]},
    ],
    "athletes": [
        {"athlete": {"displayName": "Luka Doncic", "teamName": "Lakers",
                     "position": {"abbreviation": "G"}},
         "categories": [
             {"name": "general", "values": [64, 35.7, 8.0, 34, 8, 0]},
             {"name": "offensive", "values": [33.5, 10.8, 3.98, 47.5]},
             {"name": "defensive", "values": [1.64, 0.53]},
         ]},
        {"athlete": {"displayName": "Nikola Jokic", "teamName": "Nuggets",
                     "position": {"abbreviation": "C"}},
         "categories": [
             {"name": "general", "values": [70, 34.0, 12.0, 60, 3, 0]},
             {"name": "offensive", "values": [24.0, 9.0, 3.0, 58.0]},
             {"name": "defensive", "values": [1.2, 0.8]},
         ]},
        {"athlete": {"displayName": "", "teamName": "Nobody",  # nameless -> dropped
                     "position": {"abbreviation": "F"}},
         "categories": []},
    ],
}


class TestParseByathlete:
    def test_maps_cats_positions_and_counting(self):
        players = draft.parse_byathlete(_FIXTURE)
        assert len(players) == 2  # the nameless athlete is dropped
        luka = players[0]
        assert luka["name"] == "Luka Doncic"
        assert luka["positions"] == ["G"]
        assert luka["gp"] == 64
        assert luka["cats"]["PTS"] == 33.5
        assert luka["cats"]["REB"] == 8.0
        assert luka["cats"]["AST"] == 10.8
        assert luka["cats"]["ST"] == 1.64
        assert luka["cats"]["BLK"] == 0.53
        assert luka["cats"]["TO"] == 3.98
        # DD/TD/EJCT are season-total counting cats -> ints
        assert luka["cats"]["DD"] == 34 and isinstance(luka["cats"]["DD"], int)
        assert luka["cats"]["TD"] == 8
        assert "DD" in luka["counting"]
        assert luka["season"] == "2025-26"

    def test_garbage_payload_is_safe(self):
        assert draft.parse_byathlete(None) == []
        assert draft.parse_byathlete({}) == []
        assert draft.parse_byathlete({"athletes": []}) == []

    def test_ranks_from_parsed_pool(self):
        # parser output feeds the z-score ranker end to end
        pool = draft.parse_byathlete(_FIXTURE)
        ranked = fan.rank_by_zscore(pool, fan.DEFAULT_CATEGORIES)
        assert {p["name"] for p in ranked} == {"Luka Doncic", "Nikola Jokic"}
        assert all("value" in p for p in ranked)


class TestBestAvailable:
    def test_filters_drafted_and_own_roster(self):
        ranked = [{"name": "A", "value": 5.0, "positions": ["G"]},
                  {"name": "B", "value": 4.0, "positions": ["F"]},
                  {"name": "C", "value": 3.0, "positions": ["C"]}]
        recs = draft.best_available(ranked, drafted=["A"],
                                    my_roster=[{"name": "C", "positions": ["C"]}])
        assert [p["name"] for p in recs] == ["B"]  # A drafted, C already mine

    def test_pure_bpa_when_need_weight_zero(self):
        ranked = [{"name": "Guard", "value": 5.0, "positions": ["G"]},
                  {"name": "Center", "value": 4.0, "positions": ["C"]}]
        recs = draft.best_available(ranked, need_weight=0.0)
        assert recs[0]["name"] == "Guard"  # no positional bump -> raw value order

    def test_need_bump_promotes_unfilled_position(self):
        # equal value; guard slots full (5), centers empty -> center gets bumped
        ranked = [{"name": "Guard", "value": 5.0, "positions": ["G"]},
                  {"name": "Center", "value": 5.0, "positions": ["C"]}]
        my_roster = [{"name": f"g{i}", "positions": ["G"]} for i in range(5)]
        recs = draft.best_available(ranked, my_roster=my_roster, need_weight=2.0)
        assert recs[0]["name"] == "Center"
        assert recs[0]["need_bump"] > 0

    def test_limit_caps_results(self):
        ranked = [{"name": f"P{i}", "value": float(20 - i), "positions": ["G"]}
                  for i in range(20)]
        assert len(draft.best_available(ranked, limit=5)) == 5


class TestRecommendPick:
    def _pool(self):
        return [
            {"name": "Star", "cats": {"PTS": 30.0, "AST": 8.0}, "positions": ["G"]},
            {"name": "Avg", "cats": {"PTS": 20.0, "AST": 5.0}, "positions": ["F"]},
            {"name": "Low", "cats": {"PTS": 10.0, "AST": 2.0}, "positions": ["C"]},
        ]

    def test_end_to_end_recommends_best(self):
        out = draft.recommend_pick(categories=["PTS", "AST"],
                                   _pool_fn=self._pool)
        assert out.startswith("Best available")
        assert "Star" in out

    def test_excludes_drafted(self):
        out = draft.recommend_pick(categories=["PTS", "AST"], drafted=["Star"],
                                   _pool_fn=self._pool)
        assert "Star" not in out and "Avg" in out

    def test_empty_pool_degrades(self):
        assert "couldn't fetch" in draft.recommend_pick(_pool_fn=lambda: [])


class TestFormatBoard:
    def test_empty(self):
        assert "No players" in draft.format_board([])

    def test_lists_rank_pos_value(self):
        out = draft.format_board(
            [{"name": "X", "positions": ["G"], "adj_value": 4.2}])
        assert "1. X (G)" in out and "4.2" in out


@pytest.mark.skipif(os.environ.get("WES_NBA_LIVE") != "1",
                    reason="set WES_NBA_LIVE=1 to hit ESPN's byathlete endpoint")
class TestLiveDraftPool:
    def test_pool_fetch_ranks_and_has_positions(self):
        pool = draft.draftable_pool(limit=60)
        assert len(pool) > 20  # a real page of players
        assert all("cats" in p for p in pool)
        assert any(p.get("positions") for p in pool)  # positions populate
        assert any("PTS" in p["cats"] for p in pool)  # our cats map through
        ranked = fan.rank_by_zscore(pool, fan.DEFAULT_CATEGORIES)
        assert ranked[0]["value"] > ranked[-1]["value"]
