"""Unit tests for the canonical player crosswalk (wes_players, #039).

Network-free. What is tested is the MATCHING RULES, because a wrong match is
worse than a missing one: a missing player costs a pick, a wrong one silently
values the wrong human and nothing in the output says so.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "pc"))

import wes_players as wp  # noqa: E402

NFLVERSE = [
    {"gsis_id": "00-0039139", "display_name": "Jahmyr Gibbs", "position": "RB",
     "birth_date": "2002-03-20", "espn_id": "4429795", "pfr_id": "GibbJa00",
     "last_season": "2025", "latest_team": "DET"},
    {"gsis_id": "00-0011111", "display_name": "Charlie Jones", "position": "WR",
     "birth_date": "1998-01-01", "espn_id": "111", "last_season": "2025"},
    {"gsis_id": "00-0022222", "display_name": "Charlie Jones", "position": "WR",
     "birth_date": "2000-05-05", "espn_id": "222", "last_season": "2025"},
]


def _canon():
    return wp.parse_nflverse(NFLVERSE)


class TestNameKey:
    def test_strips_punctuation_and_suffixes(self):
        assert wp.name_key("Ja'Marr Chase") == wp.name_key("JaMarr Chase")
        assert wp.name_key("Marvin Harrison Jr.") == wp.name_key("Marvin Harrison")
        assert wp.name_key("A.J. Brown") == wp.name_key("AJ Brown")

    def test_does_not_collapse_genuinely_different_names(self):
        assert wp.name_key("Josh Allen") != wp.name_key("Keenan Allen")


class TestMergeSleeper:
    def test_exact_id_wins(self):
        idx = {"9221": {"name": "Different Spelling", "positions": ["RB"],
                        "espn_id": "4429795"}}
        table, rep = wp.merge_sleeper(_canon(), idx)
        assert table["00-0039139"]["sleeper_id"] == "9221"
        assert rep["by_id"] == 1

    def test_name_plus_position_plus_dob_resolves_a_shared_name(self):
        """Two Charlie Joneses, both WR. Only the birth date separates them —
        which is exactly why it is carried through from Sleeper."""
        idx = {"55": {"name": "Charlie Jones", "positions": ["WR"],
                      "espn_id": None, "birth_date": "2000-05-05"}}
        table, rep = wp.merge_sleeper(_canon(), idx)
        assert table["00-0022222"]["sleeper_id"] == "55"
        assert table["00-0011111"]["sleeper_id"] is None
        assert rep["by_dob"] == 1

    def test_an_ambiguous_name_is_REPORTED_not_guessed(self):
        """No birth date and two candidates: refusing is right. A missing player
        costs a pick; a wrong one values the wrong human invisibly."""
        idx = {"55": {"name": "Charlie Jones", "positions": ["WR"],
                      "espn_id": None}}
        table, rep = wp.merge_sleeper(_canon(), idx)
        assert table["00-0011111"]["sleeper_id"] is None
        assert table["00-0022222"]["sleeper_id"] is None
        assert any("Charlie Jones" in a for a in rep["ambiguous"])

    def test_an_unambiguous_name_match_is_allowed(self):
        idx = {"9221": {"name": "Jahmyr Gibbs", "positions": ["RB"],
                        "espn_id": None}}
        table, rep = wp.merge_sleeper(_canon(), idx)
        assert table["00-0039139"]["sleeper_id"] == "9221"
        assert rep["by_name"] == 1

    def test_position_must_agree(self):
        """A shared name at a different position is a different person."""
        idx = {"9221": {"name": "Jahmyr Gibbs", "positions": ["WR"],
                        "espn_id": None}}
        table, _ = wp.merge_sleeper(_canon(), idx)
        assert table["00-0039139"]["sleeper_id"] is None

    def test_a_sleeper_only_player_is_KEPT_not_dropped(self):
        """Practice-squad and very recent signings are real and draftable;
        dropping them would quietly shrink the pool."""
        idx = {"999": {"name": "Undrafted Rookie", "positions": ["RB"],
                       "espn_id": None}}
        table, rep = wp.merge_sleeper(_canon(), idx)
        assert table["sleeper:999"]["sleeper_id"] == "999"
        assert "Undrafted Rookie (RB)" in rep["unmatched"]

    def test_a_weaker_rule_never_overrides_a_stronger_one(self):
        """Order is evidence-strength. An id match must not be re-decided by a
        later name match."""
        idx = {"9221": {"name": "Charlie Jones", "positions": ["RB"],
                        "espn_id": "4429795"}}
        table, rep = wp.merge_sleeper(_canon(), idx)
        assert table["00-0039139"]["sleeper_id"] == "9221"   # by id
        assert rep["by_id"] == 1 and rep["by_name"] == 0


class TestIndexBy:
    def test_builds_a_lookup_and_skips_missing_ids(self):
        table, _ = wp.merge_sleeper(
            _canon(), {"9221": {"name": "Jahmyr Gibbs", "positions": ["RB"],
                                "espn_id": "4429795"}})
        by_sleeper = wp.index_by(table, "sleeper_id")
        assert by_sleeper["9221"]["name"] == "Jahmyr Gibbs"
        assert len(by_sleeper) == 1        # the unmatched ones are not in it
