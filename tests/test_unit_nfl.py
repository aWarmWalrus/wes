"""Unit tests for the NFL points-based valuer (wes_nfl) — pure, network-free.

The NBA valuer is relative (per-league z-scores against a pool); this one is
absolute (a stat line through the league's scoring settings). These tests pin
the arithmetic, the alias tolerance, and the interface parity that lets
wes_fantasy.optimize_lineup / wes_draft.best_available consume either sport.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "pc"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import wes_nfl as nfl  # noqa: E402
import wes_fantasy as fan  # noqa: E402


class TestFantasyPoints:
    def test_scores_a_receiving_line_by_the_book(self):
        # 100 rec yds = 10, 1 rec TD = 6, 8 receptions at half PPR = 4
        cats = {"RecYds": 100, "RecTD": 1, "Rec": 8}
        assert nfl.fantasy_points(cats, "half") == 20.0
        assert nfl.fantasy_points(cats, "standard") == 16.0   # receptions free
        assert nfl.fantasy_points(cats, "ppr") == 24.0        # 1 per catch

    def test_reception_setting_is_the_only_preset_difference(self):
        std, ppr = nfl.SCORING_STANDARD, nfl.SCORING_PPR
        differing = {k for k in set(std) | set(ppr) if std.get(k) != ppr.get(k)}
        assert differing == {"Rec"}

    def test_passing_yards_are_a_quarter_point_per_ten(self):
        # Yahoo: 1 point per 25 passing yards -> 300 yds = 12
        assert nfl.fantasy_points({"PassYds": 300}) == 12.0
        assert nfl.fantasy_points({"PassYds": 300, "PassTD": 3, "Int": 1}) == 23.0

    def test_negative_stats_subtract(self):
        assert nfl.fantasy_points({"RushYds": 100, "FumLost": 2}) == 6.0
        assert nfl.fantasy_points({"Int": 3}) == -3.0

    def test_unknown_and_missing_stats_score_nothing(self):
        assert nfl.fantasy_points({"Vibes": 99}) == 0.0
        assert nfl.fantasy_points({}) == 0.0
        assert nfl.fantasy_points(None) == 0.0

    def test_non_numeric_values_are_skipped_not_fatal(self):
        assert nfl.fantasy_points({"RecYds": "n/a", "RecTD": 1}) == 6.0
        assert nfl.fantasy_points({"RecTD": True}) == 0.0   # bool is not a stat

    @pytest.mark.parametrize("alias", ["receptions", "REC", "Catches", "rec"])
    def test_stat_aliases_score_the_same(self, alias):
        assert nfl.fantasy_points({alias: 10}, "ppr") == 10.0

    def test_alias_tolerates_spaces_and_underscores(self):
        assert nfl.fantasy_points({"receiving_yards": 100}) == 10.0
        assert nfl.fantasy_points({"Receiving Yards": 100}) == 10.0

    def test_scoring_accepts_a_dict_override(self):
        custom = dict(nfl.SCORING_PPR, RecTD=10.0)
        assert nfl.fantasy_points({"RecTD": 2}, custom) == 20.0

    def test_unknown_preset_name_falls_back_to_default(self):
        assert nfl.scoring_preset("nonsense") is nfl.DEFAULT_SCORING
        assert nfl.scoring_preset(None) is nfl.DEFAULT_SCORING
        assert nfl.scoring_preset("HALF-PPR") is nfl.SCORING_HALF_PPR


class TestKickersAndDefence:
    def test_kicker_line(self):
        assert nfl.fantasy_points({"FG": 3, "XP": 2}) == 11.0

    def test_long_field_goals_pay_more(self):
        assert nfl.fantasy_points({"FG50": 1}) == 5.0
        assert nfl.fantasy_points({"FG20_29": 1}) == 3.0

    @pytest.mark.parametrize("allowed,expect", [
        (0, 10.0), (3, 7.0), (6, 7.0), (10, 4.0), (13, 4.0),
        (17, 1.0), (20, 1.0), (24, 0.0), (30, -1.0), (45, -4.0),
    ])
    def test_points_allowed_tiers(self, allowed, expect):
        assert nfl.points_allowed_value(allowed) == expect

    def test_defence_line_combines_tiers_and_events(self):
        # 3 sacks (3) + 2 INT (4) + 1 TD (6) + allowed 6 pts (7) = 20
        cats = {"Sack": 3, "DefInt": 2, "DefTD": 1, "PtsAllowed": 6}
        assert nfl.fantasy_points(cats) == 20.0

    def test_points_allowed_is_not_scored_as_a_linear_weight(self):
        """PtsAllowed must go through the tier ladder only — if it ever picked up
        a per-unit weight too it would be double-counted."""
        assert "PtsAllowed" not in nfl.SCORING_PPR
        assert nfl.fantasy_points({"PtsAllowed": 0}) == 10.0

    def test_non_numeric_points_allowed_is_ignored(self):
        assert nfl.points_allowed_value(None) == 0.0
        assert nfl.points_allowed_value("shutout") == 0.0


class TestRanking:
    POOL = [
        {"name": "Elite WR", "positions": ["WR"],
         "cats": {"RecYds": 110, "RecTD": 1, "Rec": 8}},
        {"name": "Mid RB", "positions": ["RB"],
         "cats": {"RushYds": 80, "RushTD": 0, "Rec": 2}},
        {"name": "Bust TE", "positions": ["TE"],
         "cats": {"RecYds": 20, "Rec": 2, "FumLost": 1}},
    ]

    def test_ranks_highest_points_first(self):
        ranked = nfl.rank_by_points(self.POOL, "half")
        assert [p["name"] for p in ranked] == ["Elite WR", "Mid RB", "Bust TE"]
        assert ranked[0]["value"] == 21.0

    def test_preserves_input_fields(self):
        ranked = nfl.rank_by_points(self.POOL)
        assert ranked[0]["positions"] == ["WR"]
        assert "cats" in ranked[0]

    def test_empty_pool_is_empty_not_an_error(self):
        assert nfl.rank_by_points([]) == []
        assert nfl.rank_by_points(None) == []

    def test_scoring_choice_can_reorder_the_pool(self):
        """A reception-heavy player outranks a yardage player in PPR but not in
        standard — the whole reason the preset must match the real league."""
        pool = [{"name": "Volume", "cats": {"Rec": 10, "RecYds": 60}},
                {"name": "Yardage", "cats": {"Rec": 2, "RecYds": 100}}]
        assert nfl.rank_by_points(pool, "ppr")[0]["name"] == "Volume"
        assert nfl.rank_by_points(pool, "standard")[0]["name"] == "Yardage"

    def test_single_player_is_meaningful_unlike_a_zscore(self):
        """Points are absolute, so ranking one player is valid here — the
        documented asymmetry with rank_by_zscore."""
        assert nfl.rank_by_points([self.POOL[0]])[0]["value"] > 0


class TestInterfaceParity:
    """rank_by_points output must drop straight into the shared consumers."""

    def test_ranked_pool_feeds_the_optimizer(self):
        pool = [
            {"name": "Qb", "positions": ["QB"], "cats": {"PassYds": 300, "PassTD": 3}},
            {"name": "Rb", "positions": ["RB"], "cats": {"RushYds": 100, "RushTD": 1}},
            {"name": "Wr", "positions": ["WR"], "cats": {"RecYds": 90, "Rec": 6}},
        ]
        ranked = [dict(p, playing=True, status="")
                  for p in nfl.rank_by_points(pool, "half")]
        r = fan.optimize_lineup(ranked, ["QB", "RB", "WR", "BN"])
        assert r["sport"] == "nfl"
        assert {s["slot"] for s in r["starters"]} == {"QB", "RB", "WR"}
        assert r["total"] == pytest.approx(sum(p["value"] for p in ranked))

    def test_value_key_matches_the_nba_valuers_contract(self):
        ranked = nfl.rank_by_points([{"name": "X", "cats": {"RecTD": 1}}])
        assert set(ranked[0]) >= {"name", "value"}
        assert isinstance(ranked[0]["value"], float)


class TestFormatting:
    def test_line_names_the_biggest_contributors(self):
        p = {"name": "Ja'Marr Chase", "positions": ["WR"],
             "cats": {"RecYds": 110, "RecTD": 1, "Rec": 8}}
        out = nfl.format_points(p, "half")
        assert "Ja'Marr Chase" in out and "WR" in out and "21" in out
        assert "RecYds" in out          # 11.0, the largest single contributor

    def test_degrades_on_junk_input(self):
        assert "No NFL stat line" in nfl.format_points(None)
        assert "Unknown" in nfl.format_points({})

    def test_scope_limits_are_documented(self):
        """NOT_MODELLED is load-bearing documentation, not decoration — a future
        reader must find IDP listed rather than assume it silently works."""
        assert any("IDP" in n for n in nfl.NOT_MODELLED)
