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


class TestSnakeMath:
    """Snake position arithmetic (#039). A draft assistant that can't answer
    'whose pick is this' and 'when do I pick next' is useless — with 20 picks to
    wait you can let a run happen, with 1 you cannot."""

    def test_odd_rounds_run_forward_and_even_rounds_reverse(self):
        assert [draft.slot_for_pick(i, 12) for i in range(1, 13)] == list(range(1, 13))
        assert [draft.slot_for_pick(i, 12) for i in range(13, 25)] == \
            list(range(12, 0, -1))

    def test_round_three_starts_forward_again(self):
        assert draft.slot_for_pick(25, 12) == 1

    def test_third_round_reversal_flips_the_expected_parity(self):
        """Sleeper's `reversal_round` option (0 = off in this league). Round 3
        would normally run forward; with reversal from round 3 it runs back."""
        assert draft.slot_for_pick(25, 12, reversal_round=3) == 12
        assert draft.slot_for_pick(25, 12, reversal_round=0) == 1

    def test_next_pick_skips_the_current_round_once_it_has_passed(self):
        # 5 picks made, so slot 3's round-1 pick (no. 3) is gone; next is R2.
        assert draft.next_pick_for_slot(3, 12, 5, 15) == 22

    def test_picks_until_turn_is_zero_when_on_the_clock(self):
        # 2 picks made means pick 3 is next, which belongs to slot 3.
        assert draft.picks_until_turn(3, 12, 2, 15) == 0

    def test_returns_none_once_the_draft_is_over(self):
        assert draft.next_pick_for_slot(3, 12, 12 * 15, 15) is None
        assert draft.picks_until_turn(3, 12, 12 * 15, 15) is None

    def test_degenerate_inputs_do_not_crash(self):
        assert draft.slot_for_pick(0, 12) is None
        assert draft.slot_for_pick(1, 0) is None


class TestTargetsFromSlots:
    REAL = ["QB", "RB", "RB", "WR", "WR", "TE", "W/R/T", "W/R/T", "K", "DEF",
            "BN", "BN", "BN", "BN", "BN"]

    def test_dedicated_slots_become_targets(self):
        targets, _, _ = draft.targets_from_slots(self.REAL)
        assert targets == {"QB": 1, "RB": 2, "WR": 2, "TE": 1, "K": 1, "DEF": 1}

    def test_flex_is_NOT_folded_into_each_position(self):
        """The bug this exists to prevent: folding both FLEX slots into every
        eligible position yields TE:3, and a need bump built on that keeps
        recommending a third tight end."""
        targets, flex, flex_pos = draft.targets_from_slots(self.REAL)
        assert targets["TE"] == 1
        assert flex == 2
        assert flex_pos == {"RB", "WR", "TE"}

    def test_bench_slots_are_not_targets(self):
        targets, _, _ = draft.targets_from_slots(["QB", "BN", "IR", "TAXI"])
        assert targets == {"QB": 1}

    def test_superflex_is_recognised_as_flex_not_a_position(self):
        targets, flex, flex_pos = draft.targets_from_slots(["QB", "Q/W/R/T"])
        assert targets == {"QB": 1} and flex == 1
        assert "QB" in flex_pos

    def test_derived_from_config_so_an_unusual_league_still_works(self):
        """A 3-WR league should want 3 WRs without anyone editing a table."""
        targets, _, _ = draft.targets_from_slots(["WR", "WR", "WR", "QB"])
        assert targets["WR"] == 3


class TestReplacementLevels:
    """Value over replacement (#039). Ranking by raw points put SIX
    quarterbacks in the top eight of the real 2026-08-14 board — a QB
    out-scores a back while being far easier to replace, so raw totals are the
    wrong currency for a draft pick."""

    def _pool(self):
        # 30 QBs and 60 RBs, values descending, so replacement ranks differ.
        qbs = [{"positions": ["QB"], "value": 30.0 - i * 0.2} for i in range(30)]
        rbs = [{"positions": ["RB"], "value": 25.0 - i * 0.5} for i in range(60)]
        return qbs + rbs

    def test_replacement_is_the_last_startable_player_at_the_position(self):
        # 12 teams x 1 QB slot -> the 12th best QB is replacement level.
        repl = draft.replacement_levels(
            self._pool(), {"QB": 1, "RB": 2}, 0, set(), teams=12)
        assert repl["QB"] == 30.0 - 11 * 0.2
        assert repl["RB"] == 25.0 - 23 * 0.5     # 12 x 2 = 24th best RB

    def test_a_scarce_position_yields_more_surplus_than_a_deep_one(self):
        """The whole point: the BEST QB is barely better than a startable QB,
        while the best RB is far better than the last startable RB."""
        pool = self._pool()
        repl = draft.replacement_levels(
            pool, {"QB": 1, "RB": 2}, 0, set(), teams=12)
        best_qb = max(p["value"] for p in pool if p["positions"] == ["QB"])
        best_rb = max(p["value"] for p in pool if p["positions"] == ["RB"])
        assert (best_rb - repl["RB"]) > (best_qb - repl["QB"])

    def test_flex_capacity_raises_the_bar_for_eligible_positions(self):
        """Flex slots mean more RBs start league-wide, so replacement moves
        deeper into the pool and every RB is worth less over it."""
        no_flex = draft.replacement_levels(
            self._pool(), {"RB": 2}, 0, set(), teams=12)
        with_flex = draft.replacement_levels(
            self._pool(), {"RB": 2}, 2, {"RB"}, teams=12)
        assert with_flex["RB"] < no_flex["RB"]

    def test_a_position_nobody_starts_is_all_surplus(self):
        repl = draft.replacement_levels(
            [{"positions": ["P"], "value": 5.0}], {}, 0, set(), teams=12)
        assert repl["P"] == 0.0

    def test_a_pool_shallower_than_the_slots_uses_the_worst_player(self):
        """Fewer kickers listed than the league starts: replacement is the worst
        one available, not a crash and not an invented value."""
        repl = draft.replacement_levels(
            [{"positions": ["K"], "value": 9.0},
             {"positions": ["K"], "value": 7.0}], {"K": 1}, 0, set(), teams=12)
        assert repl["K"] == 7.0

    def test_players_without_a_value_are_ignored_not_counted_as_zero(self):
        """value=None means UNKNOWN (the #029 rule). Counting it as 0 would drag
        replacement down and inflate everyone's surplus."""
        pool = [{"positions": ["QB"], "value": 20.0},
                {"positions": ["QB"], "value": None}]
        repl = draft.replacement_levels(pool, {"QB": 1}, 0, set(), teams=1)
        assert repl["QB"] == 20.0


class TestRosterFit:
    """Roster construction (#039). Value over replacement says who is BEST;
    this says who FITS. Owner, arguing against a pre-ranked queue: 'there is
    some decision making around team fit, making sure bye weeks are staggered,
    not having too many players from same team' — none of which a queue fixed
    in advance can express, because it cannot know what fell to you."""

    BYES = {"CIN": 10, "PHI": 10, "SF": 8, "KC": 6}

    def test_same_team_cap_is_HARD_not_a_penalty(self):
        """Countable, so it is a constraint rather than a judgement (#038). At
        the cap the player is excluded outright — 'one more Bengal' is not a
        matter of degree once you've taken the correlation risk."""
        mine = [{"team": "CIN"}, {"team": "CIN"}, {"team": "CIN"}]
        allowed, penalty, why = draft.roster_fit(
            {"team": "CIN", "positions": ["WR"]}, mine, self.BYES)
        assert allowed is False
        assert "already have 3 from CIN" in why[0]

    def test_approaching_the_cap_is_a_soft_nudge(self):
        mine = [{"team": "CIN"}, {"team": "CIN"}]
        allowed, penalty, why = draft.roster_fit(
            {"team": "CIN", "positions": ["WR"]}, mine, {"CIN": 10})
        assert allowed is True and penalty > 0
        assert "3rd from CIN" in why[0]      # not "3th"

    def test_bye_clustering_is_SOFT_not_hard(self):
        """Some overlap is unavoidable and harmless; the harm scales. A hard
        rule would refuse good players for a small real cost."""
        mine = [{"team": "CIN"}, {"team": "PHI"}]      # both week 10
        allowed, penalty, why = draft.roster_fit(
            {"team": "PHI", "positions": ["RB"]}, mine, self.BYES)
        assert allowed is True and penalty > 0
        assert "week-10 bye" in why[0]

    def test_penalty_grows_with_the_pile_up(self):
        two = [{"team": "CIN"}, {"team": "PHI"}]
        three = two + [{"team": "PHI"}]
        _, p2, _ = draft.roster_fit({"team": "PHI"}, two, self.BYES)
        _, p3, _ = draft.roster_fit({"team": "PHI"}, three, self.BYES)
        assert p3 > p2

    def test_a_staggered_roster_is_not_penalised(self):
        mine = [{"team": "SF"}, {"team": "KC"}]        # weeks 8 and 6
        allowed, penalty, why = draft.roster_fit(
            {"team": "CIN", "positions": ["RB"]}, mine, self.BYES)
        assert allowed is True and penalty == 0.0 and why == []

    def test_an_unknown_bye_costs_nothing(self):
        """The honest reading of missing data. Treating it as some default week
        would cluster a roster onto a week nobody is actually on bye."""
        mine = [{"team": "CIN"}, {"team": "PHI"}]
        allowed, penalty, why = draft.roster_fit(
            {"team": "ZZZ", "positions": ["RB"]}, mine, self.BYES)
        assert allowed is True and penalty == 0.0

    def test_reasons_come_back_so_the_pick_can_explain_itself(self):
        mine = [{"team": "CIN"}, {"team": "CIN"}]
        _, _, why = draft.roster_fit({"team": "CIN"}, mine, {"CIN": 10})
        assert why and all(isinstance(r, str) for r in why)


class TestMustFill:
    """The constraint that finishes a roster.

    A 15-round mock ended WR6/RB7/TE1/QB1 -- no kicker, no defense -- with the
    model correctly observing each time that a skill player had the better
    value. K and DEF have low VOR by construction, so no amount of prompting
    reverses it; the choice set has to close instead (2026-08-22).
    """

    def test_slack_means_no_constraint(self):
        assert draft.must_fill({"K": 1, "DEF": 1}, 6) == ()

    def test_the_last_picks_are_spoken_for(self):
        assert draft.must_fill({"K": 1, "DEF": 1}, 2) == ("DEF", "K")

    def test_the_final_pick_of_a_single_gap(self):
        assert draft.must_fill({"K": 1}, 1) == ("K",)

    def test_more_gaps_than_picks_still_constrains(self):
        """Overcommitted: the roster cannot be completed, but every remaining
        pick should still go at a hole rather than a twelfth running back."""
        assert draft.must_fill({"K": 1, "DEF": 1, "TE": 1}, 2) == (
            "DEF", "K", "TE")

    def test_a_full_roster_constrains_nothing(self):
        assert draft.must_fill({}, 1) == ()
        assert draft.must_fill({"K": 0}, 1) == ()

    def test_unknown_picks_left_does_not_invent_a_constraint(self):
        """None is UNKNOWN. Guessing here would force a kicker in round 2."""
        assert draft.must_fill({"K": 1}, None) == ()
