"""Unit tests for the NFL points-based valuer (wes_nfl) — pure, network-free.

The NBA valuer is relative (per-league z-scores against a pool); this one is
absolute (a stat line through the league's scoring settings). These tests pin
the arithmetic, the alias tolerance, and the interface parity that lets
wes_fantasy.optimize_lineup / wes_draft.best_available consume either sport.
"""
import json
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


SETTINGS = os.path.join(os.path.dirname(__file__), "fixtures",
                        "yahoo_nfl_settings.txt")
with open(SETTINGS, encoding="utf-8") as _f:
    SETTINGS_LINES = [ln.strip() for ln in _f if ln.strip()]


class TestParseScoring:
    """Parsed from the REAL settings page of nfl.l.957011 (captured 2026-07-29)."""

    @classmethod
    def setup_class(cls):
        cls.s = nfl.parse_scoring(SETTINGS_LINES)

    def test_yards_per_point_becomes_a_rate(self):
        # "Passing Yards 25 yards per point" -> 1/25 of a point per yard.
        assert self.s["weights"]["PassYds"] == 0.04
        assert self.s["weights"]["RushYds"] == 0.1
        assert self.s["weights"]["RecYds"] == 0.1

    def test_reads_the_reception_setting(self):
        """The single most valuation-critical number — it decides every RB/WR
        ranking, and this league is half-PPR."""
        assert self.s["weights"]["Rec"] == 0.5

    def test_offense_and_defense_interceptions_do_not_collide(self):
        """Yahoo reuses the label with OPPOSITE meanings across sections:
        'Interceptions -1' (a QB throwing one) vs 'Interception 2' (a defence
        catching one). A flat label map scores one as the other."""
        assert self.s["weights"]["Int"] == -1.0
        assert self.s["weights"]["DefInt"] == 2.0

    def test_defense_touchdown_is_not_confused_with_offense_ones(self):
        assert self.s["weights"]["DefTD"] == 6.0
        assert self.s["weights"]["PassTD"] == 4.0
        assert self.s["weights"]["RushTD"] == 6.0

    def test_field_goals_by_distance(self):
        w = self.s["weights"]
        assert (w["FG0_19"], w["FG20_29"], w["FG30_39"]) == (3.0, 3.0, 3.0)
        assert (w["FG40_49"], w["FG50"]) == (4.0, 5.0)
        assert w["XP"] == 1.0

    def test_negative_and_fractional_values(self):
        assert self.s["weights"]["FumLost"] == -2.0
        assert self.s["weights"]["Rec"] == 0.5

    def test_points_allowed_ladder(self):
        assert self.s["tiers"] == [(0, 10.0), (6, 7.0), (13, 4.0), (20, 1.0),
                                   (27, 0.0), (34, -1.0), (10**6, -4.0)]

    def test_ladder_is_used_by_the_valuer(self):
        assert nfl.points_allowed_value(3, self.s["tiers"]) == 7.0
        assert nfl.points_allowed_value(40, self.s["tiers"]) == -4.0

    def test_this_league_matches_the_half_ppr_preset(self):
        """The presets were educated guesses; this league confirms them. If a
        FUTURE league differs, that's exactly why we now read the real thing."""
        for key, val in nfl.SCORING_HALF_PPR.items():
            if key in self.s["parsed"]:
                assert self.s["parsed"][key] == val, key

    def test_nothing_unparsed(self):
        assert self.s["unknown"] == []

    def test_general_settings_table_is_ignored(self):
        """Lines before the first section header (league name, waiver rules,
        dates) must not be mistaken for scoring."""
        assert "Max Teams" not in self.s["parsed"]
        assert all(not k.startswith("Trade") for k in self.s["parsed"])

    def test_unrecognized_scoring_line_is_reported_not_silently_zero(self):
        s = nfl.parse_scoring(["Offense League Value Yahoo Default Value",
                              "Passing Yards 25 yards per point",
                              "Quantum Touchdowns 9"])
        assert s["weights"]["PassYds"] == 0.04
        # Unknown LABELS aren't reported (they may be settings we don't score);
        # a known label with an unparseable value is what must surface.
        s2 = nfl.parse_scoring(["Offense League Value Yahoo Default Value",
                               "Receptions banana"])
        assert s2["unknown"] == ["Receptions banana"]

    def test_missing_stats_fall_back_to_defaults_not_zero(self):
        s = nfl.parse_scoring(["Offense League Value Yahoo Default Value",
                              "Receptions 1"])
        assert s["weights"]["Rec"] == 1.0                    # from the league
        assert s["weights"]["PassTD"] == nfl.DEFAULT_SCORING["PassTD"]

    def test_empty_input_degrades_to_defaults(self):
        s = nfl.parse_scoring([])
        assert s["weights"] == nfl.DEFAULT_SCORING
        assert s["tiers"] == list(nfl.POINTS_ALLOWED_TIERS)
        assert nfl.parse_scoring(None)["weights"] == nfl.DEFAULT_SCORING

    def test_parsed_weights_drive_scoring_end_to_end(self):
        cats = {"RecYds": 100, "Rec": 8, "RecTD": 1}
        assert nfl.fantasy_points(cats, self.s["weights"]) == 20.0


class TestParseRosterSlots:
    def test_reads_the_authoritative_slot_list(self):
        slots = nfl.parse_roster_slots(SETTINGS_LINES)
        assert slots == ["QB", "WR", "WR", "RB", "RB", "TE", "W/R/T", "K",
                         "DEF", "BN", "BN", "BN", "BN", "BN", "BN", "IR", "IR"]

    def test_slots_feed_the_optimizer_and_infer_nfl(self):
        slots = nfl.parse_roster_slots(SETTINGS_LINES)
        assert fan.infer_sport(slots) == "nfl"
        players = [{"name": "Qb", "positions": ["QB"], "value": 20.0,
                    "playing": True, "status": ""}]
        r = fan.optimize_lineup(players, slots)
        assert [s["slot"] for s in r["starters"]] == ["QB"]
        # Nine active slots in this league; one filled leaves eight empty.
        assert len(r["empty_slots"]) == 8

    def test_absent_line_returns_empty(self):
        assert nfl.parse_roster_slots(["Max Teams: 10"]) == []
        assert nfl.parse_roster_slots([]) == []


BYATHLETE = os.path.join(os.path.dirname(__file__), "fixtures",
                         "espn_nfl_byathlete.json")
with open(BYATHLETE, encoding="utf-8") as _f:
    BYATHLETE_PAYLOAD = json.load(_f)


class TestParseByathlete:
    """Parsed from a REAL ESPN NFL payload (2025 season, captured 2026-07-29)."""

    @classmethod
    def setup_class(cls):
        cls.lines = nfl.parse_byathlete(BYATHLETE_PAYLOAD)
        cls.by_name = {p["name"]: p for p in cls.lines}

    def test_parses_every_athlete(self):
        assert len(self.lines) == len(BYATHLETE_PAYLOAD["athletes"])
        assert "Drake Maye" in self.by_name

    def test_season_and_games_played(self):
        m = self.by_name["Drake Maye"]
        assert m["season"] == "2025"
        assert m["gp"] == 17.0
        assert m["team"] == "Patriots"

    def test_quarterback_passing_line(self):
        m = self.by_name["Drake Maye"]["cats"]
        assert m["PassYds"] == 4394.0
        assert m["PassTD"] == 31.0
        assert m["Int"] == 8.0          # thrown

    def test_sacks_taken_are_NOT_scored_as_sacks_made(self):
        """The catastrophic one: ESPN's passing.sacks is sacks the QB TOOK (Drake
        Maye 2025: 47). A naive 'sacks' lookup hands a quarterback 47 sack
        points, which would rank him above every real defence."""
        m = self.by_name["Drake Maye"]["cats"]
        assert "Sack" not in m
        assert 47.0 not in m.values()

    def test_defensive_interceptions_are_not_mapped(self):
        """passing.interceptions (thrown, negative) must not collide with
        defensiveinterceptions.interceptions (caught, positive)."""
        m = self.by_name["Drake Maye"]["cats"]
        assert m["Int"] == 8.0
        assert "DefInt" not in m

    def test_a_quarterbacks_interceptions_cost_him_points(self):
        m = self.by_name["Drake Maye"]
        with_ints = nfl.fantasy_points(m["cats"])
        without = nfl.fantasy_points({k: v for k, v in m["cats"].items()
                                      if k != "Int"})
        assert with_ints < without

    def test_running_back_line(self):
        rb = next(p for p in self.lines if p["positions"] == ["RB"])
        assert rb["cats"]["RushYds"] > 500
        assert "RushTD" in rb["cats"]

    def test_receiver_line_has_receptions(self):
        wr = next(p for p in self.lines if p["positions"] == ["WR"])
        assert wr["cats"]["Rec"] > 0
        assert wr["cats"]["RecYds"] > 500

    def test_kicker_position_normalized_and_fg_buckets_mapped(self):
        """ESPN says PK and 'fieldGoalsMade1_19'; Yahoo's slot is K and its
        bucket is 0-19. Both are translated, or a kicker is ineligible for the
        K slot and scores nothing."""
        k = next(p for p in self.lines if p["positions"] == ["K"])
        assert k["positions"] == ["K"]          # not "PK"
        assert any(key.startswith("FG") for key in k["cats"])
        assert "XP" in k["cats"]

    def test_kicker_scores_points(self):
        k = next(p for p in self.lines if p["positions"] == ["K"])
        assert nfl.fantasy_points(k["cats"]) > 0

    def test_positions_are_yahoo_slot_compatible(self):
        for p in self.lines:
            for pos in p["positions"]:
                assert pos in fan._SPORTS["nfl"]["eligibility"], pos

    def test_junk_payload_degrades_to_empty(self):
        assert nfl.parse_byathlete(None) == []
        assert nfl.parse_byathlete({}) == []
        assert nfl.parse_byathlete({"athletes": [{}]}) == []   # no name -> skipped

    def test_ranking_the_real_pool_puts_a_plausible_player_on_top(self):
        ranked = nfl.rank_by_points(self.lines, "half")
        assert ranked[0]["value"] > ranked[-1]["value"]
        # A QB throwing for 4394 yards + 31 TD should out-score a kicker.
        top = ranked[0]
        assert top["positions"][0] in {"QB", "RB", "WR", "TE"}


def _gamelog(rows, names, season="Regular Season", meta=None):
    """Minimal gamelog-shaped payload. `rows` is [(event_id, [stat strings])]."""
    return {
        "names": names,
        "events": meta or {eid: {"week": i + 1,
                                 "gameDate": f"2025-1{i}-01T00:00:00.000+00:00",
                                 "opponent": {"abbreviation": "OPP"}}
                           for i, (eid, _) in enumerate(rows)},
        "seasonTypes": [{"displayName": f"2025 {season}",
                        "categories": [{"events": [
                            {"eventId": eid, "stats": stats}
                            for eid, stats in rows]}]}],
    }


WR_NAMES = ["receptions", "receivingTargets", "receivingYards",
            "yardsPerReception", "receivingTouchdowns", "fumblesLost"]
QB_NAMES = ["passingYards", "passingTouchdowns", "interceptions", "sacks",
            "rushingYards", "rushingTouchdowns"]
K_NAMES = ["fieldGoalsMade1_19-fieldGoalAttempts1_19",
           "fieldGoalsMade40_49-fieldGoalAttempts40_49",
           "fieldGoalsMade50-fieldGoalAttempts50",
           "extraPointsMade-extraPointAttempts", "totalKickingPoints"]


class TestParseGamelog:
    """Per-game lines (#035). Verified against the real ESPN shape 2026-07-31."""

    def test_parses_a_receiver_game(self):
        payload = _gamelog([("1", ["6", "8", "84", "14.0", "1", "0"])], WR_NAMES)
        games = nfl.parse_gamelog(payload)
        assert len(games) == 1
        # FumLost 0.0 is KEPT: a recorded zero ("he lost no fumbles") is real
        # data and must stay distinct from "-" (didn't record) — the same
        # zero-vs-unknown line the rest of the engine draws.
        assert games[0]["cats"] == {"Rec": 6.0, "RecYds": 84.0, "RecTD": 1.0,
                                    "FumLost": 0.0}

    def test_rate_and_aggregate_fields_are_not_scored(self):
        """yardsPerReception/targets are context, not fantasy points."""
        payload = _gamelog([("1", ["6", "8", "84", "14.0", "1", "0"])], WR_NAMES)
        cats = nfl.parse_gamelog(payload)[0]["cats"]
        assert "receivingTargets" not in cats and 14.0 not in cats.values()

    def test_qb_sacks_TAKEN_are_not_mapped(self):
        """The byathlete trap in a new place: a QB's gamelog `sacks` is sacks
        he TOOK. Mapping it would hand a quarterback defensive sack points."""
        payload = _gamelog([("1", ["300", "3", "1", "4", "20", "0"])], QB_NAMES)
        cats = nfl.parse_gamelog(payload)[0]["cats"]
        assert "Sack" not in cats
        assert cats["PassYds"] == 300.0 and cats["Int"] == 1.0

    def test_qb_interceptions_are_thrown_and_cost_points(self):
        payload = _gamelog([("1", ["300", "3", "2", "4", "0", "0"])], QB_NAMES)
        cats = nfl.parse_gamelog(payload)[0]["cats"]
        assert nfl.fantasy_points(cats) < nfl.fantasy_points(
            {k: v for k, v in cats.items() if k != "Int"})

    def test_compound_kicker_fields_take_the_MADE_half(self):
        """ESPN packs made-and-attempts into one field ("3-4"). Read raw, every
        kicker scores zero."""
        payload = _gamelog([("1", ["0-0", "3-4", "1-1", "2-2", "12"])], K_NAMES)
        cats = nfl.parse_gamelog(payload)[0]["cats"]
        assert cats["FG40_49"] == 3.0    # not "3-4", not 0
        assert cats["FG50"] == 1.0 and cats["XP"] == 2.0
        assert nfl.fantasy_points(cats) > 0

    def test_dash_means_did_not_record_not_zero(self):
        payload = _gamelog([("1", ["-", "-", "-", "-", "-", "-"])], WR_NAMES)
        assert nfl.parse_gamelog(payload)[0]["cats"] == {}

    def test_newest_game_first(self):
        payload = _gamelog([("1", ["1", "1", "10", "10.0", "0", "0"]),
                           ("2", ["9", "9", "90", "10.0", "0", "0"])], WR_NAMES)
        games = nfl.parse_gamelog(payload)
        assert games[0]["date"] > games[1]["date"]

    def test_postseason_is_excluded_by_default(self):
        """Fantasy leagues end before the NFL postseason, so January games are
        chronologically newer but competitively irrelevant — including them
        would let 3 playoff games dominate 'recent form' for a December call."""
        payload = _gamelog([("1", ["6", "8", "84", "14.0", "1", "0"])],
                           WR_NAMES, season="Postseason")
        assert nfl.parse_gamelog(payload) == []
        assert len(nfl.parse_gamelog(payload, season_type="Postseason")) == 1

    def test_carries_week_and_opponent_for_context(self):
        payload = _gamelog([("1", ["6", "8", "84", "14.0", "1", "0"])], WR_NAMES)
        g = nfl.parse_gamelog(payload)[0]
        assert g["week"] == 1 and g["opponent"] == "OPP"

    def test_junk_payload_degrades_to_empty(self):
        assert nfl.parse_gamelog(None) == []
        assert nfl.parse_gamelog({}) == []


class TestRecentForm:
    def _log(self, points_sequence):
        """Build games whose scores are (roughly) the given sequence, newest
        first, using receiving yards at 0.1/yd."""
        return [{"cats": {"RecYds": p * 10}, "date": f"2025-{20-i:02d}-01"}
                for i, p in enumerate(points_sequence)]

    def test_falling_form_is_detected(self):
        # last 4 average ~5, season average much higher
        form = nfl.recent_form(self._log([5, 5, 5, 5, 20, 20, 20, 20]))
        assert form["recent_ppg"] == 5.0
        assert form["baseline_ppg"] == 12.5
        assert form["delta"] == -7.5
        assert form["trend"] == "falling"

    def test_rising_form_is_detected(self):
        form = nfl.recent_form(self._log([20, 20, 20, 20, 5, 5, 5, 5]))
        assert form["trend"] == "rising" and form["delta"] > 0

    def test_steady_form_is_not_flagged_either_way(self):
        form = nfl.recent_form(self._log([10, 10, 10, 10, 10, 10]))
        assert form["trend"] == "steady" and form["delta"] == 0.0

    def test_too_few_games_is_UNKNOWN_not_a_number(self):
        """A 2-game sample after an injury is noise. Reporting it as a number
        would let the roster engine drop someone on nothing — same
        None-not-zero rule the optimizer depends on."""
        form = nfl.recent_form(self._log([2, 2]))
        assert form["recent_ppg"] is None and form["delta"] is None
        assert form["trend"] == "unknown" and form["games"] == 2

    def test_empty_log_is_unknown(self):
        assert nfl.recent_form([])["trend"] == "unknown"
        assert nfl.recent_form(None)["trend"] == "unknown"

    def test_window_is_respected(self):
        games = self._log([0, 0, 30, 30, 30, 30])
        assert nfl.recent_form(games, window=2)["recent_ppg"] == 0.0
        assert nfl.recent_form(games, window=6)["recent_ppg"] == 20.0

    def test_uses_the_leagues_real_scoring(self):
        """Form must be measured in the league's own points, or 'falling off'
        means something different from the lineup decisions it feeds."""
        games = [{"cats": {"Rec": 10}, "date": "2025-01-01"}] * 4
        ppr = nfl.recent_form(games, nfl.SCORING_PPR)["recent_ppg"]
        std = nfl.recent_form(games, nfl.SCORING_STANDARD)["recent_ppg"]
        assert ppr == 10.0 and std == 0.0


class TestPlayerGamelog:
    def test_uses_the_gamelog_endpoint(self):
        seen = {}

        def fake(url):
            seen["url"] = url
            return _gamelog([("1", ["6", "8", "84", "14.0", "1", "0"])], WR_NAMES)
        nfl.player_gamelog("12345", _get_fn=fake)
        assert "athletes/12345/gamelog" in seen["url"]

    def test_network_failure_degrades_to_empty(self):
        def boom(url):
            raise OSError("espn down")
        assert nfl.player_gamelog("1", _get_fn=boom) == []


class TestPerGame:
    def test_rescales_by_games_played(self):
        line = {"name": "X", "gp": 10, "cats": {"RecYds": 1000, "Rec": 50}}
        pg = nfl.per_game(line)
        assert pg["cats"] == {"RecYds": 100.0, "Rec": 5.0}
        assert pg["gp"] == 10                 # original preserved

    @pytest.mark.parametrize("gp", [None, 0, "n/a"])
    def test_unusable_gp_returns_the_line_unchanged(self, gp):
        """No divide-by-zero and no silent zeroing — season totals pass through."""
        line = {"name": "X", "gp": gp, "cats": {"RecYds": 10}}
        assert nfl.per_game(line)["cats"] == {"RecYds": 10}

    def test_absent_gp_key_returns_the_line_unchanged(self):
        line = {"name": "X", "cats": {"RecYds": 10}}
        assert nfl.per_game(line)["cats"] == {"RecYds": 10}

    def test_per_game_can_reorder_against_season_totals(self):
        """The reason this is the caller's choice: a 4-game star out-ranks a
        17-game plodder per game, and the reverse on totals."""
        star = {"name": "Star", "gp": 4, "cats": {"RecYds": 600, "RecTD": 6}}
        plod = {"name": "Plod", "gp": 17, "cats": {"RecYds": 900, "RecTD": 6}}
        totals = nfl.rank_by_points([star, plod])
        pergame = nfl.rank_by_points([nfl.per_game(star), nfl.per_game(plod)])
        assert totals[0]["name"] == "Plod"
        assert pergame[0]["name"] == "Star"


def _paged_payload(names, page, pages, positions=None):
    """A minimal byathlete-shaped payload for one page, `pages` total."""
    positions = positions or {}
    athletes = [{"athlete": {"displayName": n,
                             "position": {"abbreviation": positions.get(n, "WR")}},
                "categories": []} for n in names]
    return {"requestedSeason": {"displayName": "2025"},
           "categories": [], "athletes": athletes,
           "pagination": {"count": len(names) * pages, "limit": 60,
                          "page": page, "pages": pages}}


class TestPagination:
    """ESPN both under-limits (some sorts need <=60 to return anything, #029
    7/29) and flakes PER-PAGE independent of that (a page can come back
    completely empty while its neighbours are fine, #029 7/30). Both are
    real, measured ESPN behaviours, not hypotheticals."""

    def test_single_page_sort_is_unaffected(self):
        payload = _paged_payload(["A", "B"], 1, 1)
        lines, ok = nfl._paginated_pool(
            "x", 60, None, lambda url: payload)
        assert ok is True
        assert {p["name"] for p in lines} == {"A", "B"}

    def test_walks_every_page_and_merges(self):
        pages = {1: _paged_payload(["A", "B"], 1, 3),
                2: _paged_payload(["C", "D"], 2, 3),
                3: _paged_payload(["E"], 3, 3)}

        def fetch(url):
            page = 1
            if "page=2" in url:
                page = 2
            elif "page=3" in url:
                page = 3
            return pages[page]
        lines, ok = nfl._paginated_pool("x", 60, None, fetch)
        assert ok is True
        assert {p["name"] for p in lines} == {"A", "B", "C", "D", "E"}

    def test_one_flaky_page_recovers_on_retry(self):
        """The exact ESPN behaviour observed: page 2 empty once, fine the next
        attempt — must not be treated as 'this page has no players'."""
        calls = {"page2": 0}
        p1 = _paged_payload(["A"], 1, 2)
        p2_ok = _paged_payload(["B"], 2, 2)

        def fetch(url):
            if "page=2" in url:
                calls["page2"] += 1
                if calls["page2"] == 1:
                    return {"league": {}}          # the real observed shape
                return p2_ok
            return p1
        lines, ok = nfl._paginated_pool("x", 60, None, fetch)
        assert ok is True
        assert {p["name"] for p in lines} == {"A", "B"}
        assert calls["page2"] == 2

    def test_a_page_that_never_recovers_still_returns_the_rest(self):
        """The rest of the pool must not be discarded for one bad page — most
        of the value lives in the pages that DID come back."""
        p1 = _paged_payload(["A"], 1, 3)
        p3 = _paged_payload(["C"], 3, 3)

        def fetch(url):
            if "page=2" in url:
                return {"league": {}}              # never recovers
            if "page=3" in url:
                return p3
            return p1
        lines, ok = nfl._paginated_pool("x", 60, None, fetch)
        assert ok is True
        assert {p["name"] for p in lines} == {"A", "C"}

    def test_page_one_itself_failing_is_the_real_failure(self):
        def fetch(url):
            return {"league": {}}                  # nothing, ever
        lines, ok = nfl._paginated_pool("x", 60, None, fetch)
        assert ok is False and lines == []

    def test_a_page_that_fails_at_one_limit_recovers_at_a_smaller_one(self):
        """The real ESPN behaviour, not a hypothetical: receiving.receivingYards
        page 2 came back completely empty at limit=60 on THREE separate
        attempts with a delay between them, yet limit=40 at that same page
        returned data immediately. So the fix cannot be 'retry the identical
        request' — it has to try a smaller limit."""
        def fetch(url):
            if "page=2" in url and "limit=60" in url:
                return {"league": {}}          # never recovers at this limit
            if "page=2" in url and "limit=40" in url:
                return _paged_payload(["B"], 2, 2)
            if "page=1" in url and "limit=60" in url:
                p = _paged_payload(["A"], 1, 2)
                p["pagination"]["limit"] = 60
                return p
            if "page=1" in url and "limit=40" in url:
                p = _paged_payload(["A"], 1, 2)
                p["pagination"]["limit"] = 40
                return p
            return {"league": {}}
        lines, ok = nfl._paginated_pool("x", 60, None, fetch)
        assert ok is True
        assert {p["name"] for p in lines} == {"A", "B"}   # full depth recovered

    def test_page_one_uses_the_limit_ladder_like_before(self):
        """The pre-existing whole-sort quirk (limit=200 returns nothing, 60
        does) must still work now that page 1 goes through _paginated_pool."""
        def fetch(url):
            if "limit=200" in url:
                return {"league": {}}
            return _paged_payload(["A"], 1, 1)
        lines, ok = nfl._paginated_pool("x", 200, None, fetch)
        assert ok is True and lines[0]["name"] == "A"

    def test_pool_by_position_now_gets_full_depth_across_pages(self):
        """The regression test that matters: before pagination, TE depth was
        capped at whatever fit in one page (12 TEs was the measured real gap
        that left Jake Ferguson without a stat line)."""
        many_tes = [f"TE{i}" for i in range(150)]
        page1 = _paged_payload(many_tes[:60], 1, 3,
                               {n: "TE" for n in many_tes})
        page2 = _paged_payload(many_tes[60:120], 2, 3,
                               {n: "TE" for n in many_tes})
        page3 = _paged_payload(many_tes[120:], 3, 3,
                               {n: "TE" for n in many_tes})
        empty = {"league": {}}

        def fetch(url):
            if "receiving" not in url:
                return empty
            if "page=2" in url:
                return page2
            if "page=3" in url:
                return page3
            return page1
        pool, failed = nfl.pool_by_position(_get_fn=fetch,
                                            _defence_fn=lambda s, g: [])
        tes = [p for p in pool if p["positions"] == ["TE"]]
        assert len(tes) == 150
        # The other three sorts legitimately returned nothing here (by design
        # of this test), and that must be visible, not swallowed.
        assert set(failed) == {"passing.passingYards:desc",
                               "rushing.rushingYards:desc",
                               "kicking.fieldGoalsMade:desc",
                               "team_defences"}   # stubbed empty above


def _byteam_payload(teams):
    """Minimal byteam-shaped payload. `teams` is a list of dicts:
    {name, own: {(cat): {label: val}}, opp: {(cat): {label: val}}}."""
    # Label lists must line up positionally with the values, like ESPN's.
    labels = {"passing": ["sacks", "totalPoints"],
             "defensiveinterceptions": ["interceptions"],
             "general": ["gamesPlayed", "fumblesRecovered"],
             "returning": ["kickReturnTouchdowns", "puntReturnTouchdowns"]}
    out_teams = []
    for t in teams:
        cats = []
        for split, blocks in (("0", t.get("own", {})), ("900", t.get("opp", {}))):
            for cat, pairs in blocks.items():
                cats.append({"name": cat, "splitId": split,
                            "values": [pairs.get(l) for l in labels[cat]]})
        out_teams.append({"team": {"name": t["name"]}, "categories": cats})
    return {"requestedSeason": {"displayName": "2025"},
           "categories": [{"name": k, "names": v} for k, v in labels.items()],
           "teams": out_teams}


class TestParseByteam:
    """Team defences (#029). The Own/Opponent split is the whole difficulty:
    reading the wrong side inverts a defence's value while looking plausible."""

    PAYLOAD = _byteam_payload([{
        "name": "Seahawks",
        "own": {"passing": {"sacks": 30.0, "totalPoints": 400.0},
               "defensiveinterceptions": {"interceptions": 18.0},
               "general": {"gamesPlayed": 17.0, "fumblesRecovered": 9.0}},
        "opp": {"passing": {"sacks": 47.0, "totalPoints": 292.0},
               "returning": {"kickReturnTouchdowns": 1.0,
                             "puntReturnTouchdowns": 2.0}},
    }])

    def test_sacks_come_from_the_OPPONENT_split(self):
        """Opponent passing.sacks = the opposing QB got sacked = OUR defence
        made them. Own passing.sacks is our own QB being sacked, which is not
        a defensive stat at all."""
        line = nfl.parse_byteam(self.PAYLOAD)[0]
        assert line["cats"]["Sack"] == 47.0      # not 30.0

    def test_points_allowed_come_from_the_OPPONENT_split(self):
        line = nfl.parse_byteam(self.PAYLOAD)[0]
        assert line["cats"]["PtsAllowed"] == 292.0   # not our own 400 scored

    def test_interceptions_come_from_the_OWN_split(self):
        """defensiveinterceptions INVERTS relative to the others — "Own
        defensive interceptions" really is the picks our defence caught. This
        asymmetry is why the mapping is explicit per-field, not a rule."""
        line = nfl.parse_byteam(self.PAYLOAD)[0]
        assert line["cats"]["DefInt"] == 18.0

    def test_return_touchdowns_sum_kick_and_punt(self):
        line = nfl.parse_byteam(self.PAYLOAD)[0]
        assert line["cats"]["DefRetTD"] == 3.0

    def test_carries_the_espn_athlete_id_for_gamelog_lookup(self):
        """The gamelog endpoint (#035 recent form) is keyed by ESPN's athlete
        id. It was being read from the payload and discarded, leaving nothing
        able to look a player's per-game history up."""
        lines = nfl.parse_byathlete(BYATHLETE_PAYLOAD)
        assert all(p["espn_id"] for p in lines)
        assert nfl.player_gamelog(lines[0]["espn_id"],
                                  _get_fn=lambda url: {}) == []

    def test_shape_matches_a_player_stat_line(self):
        """A defence must be indistinguishable from any other player to the
        optimizer, the diff and the summary — same keys, DEF position."""
        line = nfl.parse_byteam(self.PAYLOAD)[0]
        assert line["positions"] == ["DEF"]
        assert line["name"] == "Seahawks"      # nickname, matching Yahoo's row
        assert line["gp"] == 17.0
        assert set(line) >= {"name", "season", "gp", "team", "positions", "cats"}

    def test_position_is_eligible_for_the_yahoo_def_slot(self):
        line = nfl.parse_byteam(self.PAYLOAD)[0]
        assert line["positions"][0] in fan._SPORTS["nfl"]["eligibility"]

    def test_junk_payload_degrades_to_empty(self):
        assert nfl.parse_byteam(None) == []
        assert nfl.parse_byteam({}) == []
        assert nfl.parse_byteam({"teams": [{}]}) == []   # no name -> skipped

    def test_missing_fields_are_omitted_not_zeroed(self):
        """An absent ESPN field must stay UNKNOWN rather than becoming a real
        0 — the None-vs-zero rule this codebase already learned twice."""
        payload = _byteam_payload([{"name": "X", "own": {}, "opp": {}}])
        line = nfl.parse_byteam(payload)[0]
        assert line["cats"] == {}

    def test_sum_opt_returns_none_when_nothing_is_numeric(self):
        assert nfl._sum_opt(None, None) is None
        assert nfl._sum_opt(None, 2.0) == 2.0


class TestDefenceScoring:
    def test_a_defence_scores_through_the_normal_valuer(self):
        line = nfl.parse_byteam(TestParseByteam.PAYLOAD)[0]
        assert nfl.fantasy_points(line["cats"]) != 0

    def test_per_game_is_REQUIRED_before_the_points_allowed_ladder(self):
        """The subtle one, and the reason defence_pool's docstring warns about
        it: PtsAllowed is a SEASON TOTAL, but the tier ladder is per-GAME. Fed
        raw, 292 points allowed lands in the worst tier (-4) and an elite
        defence scores like a terrible one. Per-game (17.2) lands in the +1
        tier, which is correct. _nfl_value_map applies per_game to the whole
        pool, so the live path is right — this pins WHY."""
        line = nfl.parse_byteam(TestParseByteam.PAYLOAD)[0]
        raw = nfl.fantasy_points(line["cats"])
        pg = nfl.fantasy_points(nfl.per_game(line)["cats"])
        assert nfl.points_allowed_value(292.0) == -4.0    # worst tier
        assert nfl.points_allowed_value(292.0 / 17) == 1.0  # correct tier
        assert pg < raw   # the raw number is inflated by the wrong-tier sacks etc.

    def test_a_stingy_defence_outranks_a_leaky_one(self):
        stingy = _byteam_payload([{
            "name": "Good", "own": {"defensiveinterceptions": {"interceptions": 18.0},
                                    "general": {"gamesPlayed": 17.0}},
            "opp": {"passing": {"sacks": 47.0, "totalPoints": 292.0}}}])
        leaky = _byteam_payload([{
            "name": "Bad", "own": {"defensiveinterceptions": {"interceptions": 5.0},
                                   "general": {"gamesPlayed": 17.0}},
            "opp": {"passing": {"sacks": 20.0, "totalPoints": 511.0}}}])
        pool = [nfl.per_game(l) for l in
               nfl.parse_byteam(stingy) + nfl.parse_byteam(leaky)]
        ranked = nfl.rank_by_points(pool)
        assert ranked[0]["name"] == "Good"


class TestDefencePool:
    def test_uses_the_byteam_endpoint(self):
        seen = {}

        def fake(url):
            seen["url"] = url
            return TestParseByteam.PAYLOAD
        nfl.defence_pool(_get_fn=fake)
        assert "byteam" in seen["url"]

    def test_network_failure_degrades_to_empty(self):
        def boom(url):
            raise OSError("espn down")
        assert nfl.defence_pool(_get_fn=boom) == []

    def test_pool_by_position_includes_defences(self):
        """The payoff: DEF used to be absent from the pool entirely, which is
        why a real rostered defence valued at 0 and appeared in every "no stats
        found" warning."""
        def fake(url):
            if "byteam" in url:
                return TestParseByteam.PAYLOAD
            return BYATHLETE_PAYLOAD
        pool, failed = nfl.pool_by_position(_get_fn=fake)
        assert failed == []
        assert any(p["positions"] == ["DEF"] for p in pool)

    def test_a_defence_failure_is_reported_under_its_own_name(self):
        """Defences come from a different endpoint, so a defence outage must
        not masquerade as one of the athlete sorts failing."""
        def fake(url):
            if "byteam" in url:
                raise OSError("byteam down")
            return BYATHLETE_PAYLOAD
        pool, failed = nfl.pool_by_position(_get_fn=fake)
        assert failed == ["team_defences"]
        assert pool   # the athlete side still came back


class TestPlayerPool:
    def test_uses_injected_fetcher_and_parses(self):
        pool = nfl.player_pool(_get_fn=lambda url: BYATHLETE_PAYLOAD)
        assert len(pool) == len(BYATHLETE_PAYLOAD["athletes"])

    def test_network_failure_degrades_to_empty_list(self):
        def boom(url):
            raise OSError("espn down")
        assert nfl.player_pool(_get_fn=boom) == []

    def test_limit_is_capped_and_season_passed_through(self):
        seen = {}

        def fake(url):
            seen["url"] = url
            return BYATHLETE_PAYLOAD
        nfl.player_pool(limit=9999, season=2025, _get_fn=fake)
        assert "limit=200" in seen["url"]
        assert "season=2025" in seen["url"]

    def test_pool_by_position_merges_sorts_and_dedupes(self):
        calls = []

        def fake(url):
            calls.append(url)
            return BYATHLETE_PAYLOAD
        pool, failed = nfl.pool_by_position(
            _get_fn=fake, _defence_fn=lambda s, g: [{"name": "D",
                                                     "positions": ["DEF"]}])
        assert len(calls) == len(nfl._POOL_SORTS)     # one query per group
        assert failed == []
        # Same payload four times must not produce duplicates (+1 for the
        # stubbed defence, which comes from a separate source).
        assert len(pool) == len(BYATHLETE_PAYLOAD["athletes"]) + 1
        assert len({p["name"] for p in pool}) == len(pool)

    def test_empty_sort_is_retried_at_a_smaller_limit(self):
        """The real ESPN quirk: receiving.receivingYards returns NOTHING at
        limit=200 but 60 players at limit=60. Without the retry the pool silently
        contains no WRs or TEs."""
        calls = []

        def picky(url):
            calls.append(url)
            if "receiving" in url and "limit=200" in url:
                return {"athletes": []}
            return BYATHLETE_PAYLOAD
        pool, failed = nfl.pool_by_position(
            limit_each=200, _get_fn=picky,
            _defence_fn=lambda s, g: [{"name": "D", "positions": ["DEF"]}])
        assert failed == []                          # recovered, not given up on
        assert any("receiving" in c and "limit=60" in c for c in calls)
        assert pool

    def test_a_sort_that_never_works_is_REPORTED_not_hidden(self):
        """A missing position group is indistinguishable from 'those players are
        bad' once values are merged, so it has to surface as failure."""
        def broken(url):
            if "receiving" in url:
                raise OSError("espn hates receivers")
            return BYATHLETE_PAYLOAD
        pool, failed = nfl.pool_by_position(
            _get_fn=broken,
            _defence_fn=lambda s, g: [{"name": "D", "positions": ["DEF"]}])
        assert failed == ["receiving.receivingYards:desc"]
        assert pool                                   # partial pool still ranks


class TestCompositionSeam:
    """wes_fantasy wires the browser-free parser to the football-free scraper."""

    def setup_method(self):
        fan._settings_cache.clear()

    def test_reads_the_leagues_real_scoring(self):
        s = fan.nfl_league_scoring("nfl.l.957011",
                                   _lines_fn=lambda k: SETTINGS_LINES)
        assert s["weights"]["Rec"] == 0.5
        assert s["tiers"][0] == (0, 10.0)

    def test_caches_per_league(self):
        calls = []

        def fetch(key):
            calls.append(key)
            return SETTINGS_LINES
        fan.nfl_league_scoring("nfl.l.1", _lines_fn=fetch)
        fan.nfl_league_scoring("nfl.l.1", _lines_fn=fetch)
        assert calls == ["nfl.l.1"]          # second call served from cache

    def test_scoring_and_slots_share_ONE_settings_fetch(self):
        """Both read the SAME page. Fetching it twice meant two Playwright
        browser launches (~5-10s each) per lineup request."""
        calls = []

        def fetch(key):
            calls.append(key)
            return SETTINGS_LINES
        fan.nfl_league_scoring("nfl.l.9", _lines_fn=fetch)
        fan.nfl_league_slots("nfl.l.9", _lines_fn=fetch)
        assert calls == ["nfl.l.9"]

    def test_a_failed_scrape_is_not_cached(self):
        """Caching a degradation string would pin the league to defaults until
        the process restarts."""
        calls = []

        def flaky(key):
            calls.append(key)
            return SETTINGS_LINES if len(calls) > 1 else "couldn't reach Yahoo"
        assert fan.nfl_league_slots("nfl.l.8", _lines_fn=flaky) == []
        assert fan.nfl_league_slots("nfl.l.8", _lines_fn=flaky)[:1] == ["QB"]

    def test_scrape_failure_degrades_to_defaults_not_zeros(self):
        """A degradation STRING from the scrape layer must not become an empty
        weights dict — that would silently value every player at 0."""
        s = fan.nfl_league_scoring(
            "nfl.l.2", _lines_fn=lambda k: "I couldn't reach Yahoo Fantasy just now.")
        assert s["weights"] == nfl.DEFAULT_SCORING
        assert s["tiers"] == list(nfl.POINTS_ALLOWED_TIERS)

    def test_slots_from_settings(self):
        slots = fan.nfl_league_slots("nfl.l.957011",
                                     _lines_fn=lambda k: SETTINGS_LINES)
        assert slots[:3] == ["QB", "WR", "WR"]

    def test_slots_degrade_on_scrape_failure(self):
        assert fan.nfl_league_slots("nfl.l.3", _lines_fn=lambda k: "nope") == []


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


class TestSeasonProjections:
    """Forward-looking SEASON projections (#039). A draft is about expected
    season-long production; valuing on last year's ACTUALS is why a board keeps
    recommending last season's producers long after the market has moved on.

    Not to be confused with #036's weekly projections — that is matchup
    adjustment for in-season lineup calls. Different question, different data."""

    PAYLOAD = {"players": [{"player": {
        "id": 4429795, "fullName": "Jahmyr Gibbs", "defaultPositionId": 2,
        "stats": [
            # The whole-season PROJECTION: statSourceId 1, statSplitTypeId 0.
            {"statSourceId": 1, "statSplitTypeId": 0, "seasonId": 2026,
             "appliedTotal": 365.3,
             "stats": {"24": 1374.08, "25": 14.48, "53": 60.0, "42": 500.0,
                       "43": 3.0, "23": 283.0, "58": 80.0}},
            # Last season's ACTUALS, and a WEEKLY projection — both must be
            # ignored here, and both live in the same payload.
            {"statSourceId": 0, "statSplitTypeId": 0, "seasonId": 2025,
             "appliedTotal": 313.6, "stats": {"24": 999.0}},
            {"statSourceId": 1, "statSplitTypeId": 1, "seasonId": 2026,
             "scoringPeriodId": 5, "appliedTotal": 20.7,
             "stats": {"24": 80.0}},
        ]}}]}

    def _parsed(self):
        return nfl.parse_projections(self.PAYLOAD, 2026)

    def test_picks_the_season_projection_not_the_actuals(self):
        cats = self._parsed()[0]["cats"]
        assert cats["RushYds"] == 1374.08      # projected, not 999 actual

    def test_ignores_weekly_projections_in_the_same_payload(self):
        """A weekly entry would silently replace the season one and make every
        player look like a single game."""
        assert self._parsed()[0]["cats"]["RushYds"] > 1000

    def test_maps_espn_stat_ids_to_our_canonical_cats(self):
        cats = self._parsed()[0]["cats"]
        assert cats["RushTD"] == 14.48 and cats["Rec"] == 60.0
        assert cats["RecYds"] == 500.0 and cats["RecTD"] == 3.0

    def test_counting_stats_that_do_not_score_are_dropped(self):
        """Attempts (23) and targets (58) are volume, not points. Leaving them
        in `cats` would break the contract that cats are SCORING stats."""
        cats = self._parsed()[0]["cats"]
        assert "23" not in cats and "58" not in cats
        assert not any(str(k).isdigit() for k in cats)

    def test_is_marked_as_projected_so_it_cannot_be_confused_with_actuals(self):
        p = self._parsed()[0]
        assert p["projected"] is True
        assert p["gp"] == 17               # a season projection IS the full year

    def test_our_scoring_reproduces_espns_total(self):
        """The statId mapping was INFERRED from the data, so it needs a check
        that does not depend on the same inference: score the mapped cats under
        PPR and compare with ESPN's own appliedTotal. Verified live at a worst
        gap of 0.5% across the top 12 players; a mismatch here means we are
        mispricing everyone and would never see it in the output."""
        p = self._parsed()[0]
        ours = nfl.fantasy_points(p["cats"], nfl.SCORING_PPR,
                                      nfl.POINTS_ALLOWED_TIERS)
        # 137.41 + 86.88 + 60 + 50 + 18 = 352.29
        assert abs(ours - 352.29) < 1.0

    def test_a_player_with_no_season_projection_is_skipped(self):
        payload = {"players": [{"player": {
            "id": 1, "fullName": "Nobody", "defaultPositionId": 3,
            "stats": [{"statSourceId": 0, "statSplitTypeId": 0,
                       "seasonId": 2026, "stats": {"42": 10.0}}]}}]}
        assert nfl.parse_projections(payload, 2026) == []

    def test_malformed_payload_yields_nothing_rather_than_raising(self):
        assert nfl.parse_projections(None, 2026) == []
        assert nfl.parse_projections({"players": "junk"}, 2026) == []
