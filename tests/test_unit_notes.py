"""Unit tests for qualitative player notes (wes_notes, #040).

PURE: no network, no clock -- `now` is passed in, so the same inputs always
give the same note. Everything here is derived from data we already hold, and
the tests exist mostly to pin the boundary: a note we cannot support must come
out EMPTY rather than plausible.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "pc"))

import wes_notes as notes  # noqa: E402

NOW = 1_787_000_000.0


class TestInjuryNote:
    def test_a_healthy_player_gets_nothing(self):
        assert notes.injury_note({}) == ""

    def test_it_names_the_body_part_and_the_note(self):
        """"PUP" and "PUP - Knee - ACL (Surgery)" are different facts, and the
        difference is the whole question of whether to draft him."""
        got = notes.injury_note({"injury_status": "PUP",
                                 "injury_body_part": "Knee - ACL",
                                 "injury_notes": "Surgery"})
        assert "PUP" in got and "Knee - ACL" in got and "Surgery" in got

    def test_undisclosed_is_not_repeated_as_detail(self):
        """Sleeper's placeholder for "we do not know" adds nothing to the word
        already printed."""
        got = notes.injury_note({"injury_status": "Questionable",
                                 "injury_body_part": "Undisclosed"})
        assert "Undisclosed" not in got
        assert "no detail" in got

    def test_a_note_without_a_body_part_still_shows(self):
        got = notes.injury_note({"injury_status": "Out",
                                 "injury_notes": "Concussion protocol"})
        assert "Concussion protocol" in got

    def test_stale_news_is_flagged(self):
        """Nothing for three weeks reads very differently from a report this
        morning."""
        got = notes.injury_note(
            {"injury_status": "IR", "injury_body_part": "Achilles",
             "news_updated": int((NOW - 21 * 86400) * 1000)}, now=NOW)
        assert "last news" in got and "weeks ago" in got

    def test_fresh_news_is_not_mentioned(self):
        got = notes.injury_note(
            {"injury_status": "IR", "injury_body_part": "Achilles",
             "news_updated": int((NOW - 3600) * 1000)}, now=NOW)
        assert "last news" not in got

    def test_no_clock_means_no_relative_time(self):
        """PURE: without `now` there is no "days ago" to compute, and it must
        not invent one."""
        got = notes.injury_note({"injury_status": "IR",
                                 "injury_body_part": "Achilles",
                                 "news_updated": 1})
        assert "ago" not in got


class TestSeverity:
    def test_it_glosses_the_status(self):
        assert "season" in notes.severity({"injury_status": "IR"})
        assert notes.severity({"injury_status": "Questionable"}) \
            == "game-time decision"

    def test_an_unknown_status_is_empty_not_guessed(self):
        assert notes.severity({"injury_status": "Sniffles"}) == ""
        assert notes.severity({}) == ""


class TestTrajectory:
    INFO = {"age": 27, "years_exp": 5}

    def test_it_reads_the_arc_off_real_production(self):
        got = notes.trajectory(self.INFO, [(2023, 14.2), (2024, 11.8),
                                           (2025, 9.4)])
        assert "trending down" in got and "14.2" in got and "9.4" in got

    def test_a_rising_player(self):
        got = notes.trajectory({"age": 22, "years_exp": 1},
                               [(2024, 6.0), (2025, 12.0)])
        assert "trending up" in got

    def test_direction_comes_from_the_MOST_RECENT_move(self):
        """First-against-last called Puka Nacua "declining" on
        28.4 -> 10.2 -> 21.9 -- arithmetically true and useless, because what
        a manager wants to know is that he bounced back."""
        got = notes.trajectory({"age": 25, "years_exp": 3},
                               [(2023, 28.4), (2024, 10.2), (2025, 21.9)])
        assert "trending up" in got

    def test_a_wild_swing_is_called_volatile(self):
        got = notes.trajectory({"age": 25, "years_exp": 3},
                               [(2023, 28.4), (2024, 10.2), (2025, 21.9)])
        assert "volatile" in got

    def test_a_smooth_arc_is_not_called_volatile(self):
        got = notes.trajectory(self.INFO, [(2023, 12.0), (2024, 11.0),
                                           (2025, 10.0)])
        assert "volatile" not in got

    def test_a_flat_arc_is_steady_not_a_trend(self):
        got = notes.trajectory(self.INFO, [(2024, 10.0), (2025, 10.3)])
        assert "steady" in got

    def test_one_season_gives_no_direction(self):
        """An unknown arc must read as unknown, never as "flat"."""
        got = notes.trajectory(self.INFO, [(2025, 10.0)])
        assert "steady" not in got and "trending" not in got
        assert "27" in got

    def test_no_seasons_still_describes_who_he_is(self):
        assert notes.trajectory(self.INFO, []) == "27, 6th year"

    def test_a_rookie_is_named_as_one(self):
        assert "rookie" in notes.trajectory({"age": 22, "years_exp": 0}, [])

    def test_nothing_known_is_empty(self):
        assert notes.trajectory({}, []) == ""


class TestRoleNote:
    def test_a_starter_says_so(self):
        got = notes.role_note({"depth_chart_order": 1, "positions": ["RB"]}, [])
        assert got == "RB1 — starter"

    def test_a_backup_names_the_man_ahead(self):
        """This is the handcuff fact, in words a human can check."""
        got = notes.role_note(
            {"depth_chart_order": 2, "positions": ["RB"]},
            [{"name": "Saquon Barkley", "depth_chart_order": 1}])
        assert got == "RB2 behind Saquon Barkley"

    def test_it_names_the_STARTER_not_whoever_is_nearest(self):
        got = notes.role_note(
            {"depth_chart_order": 3, "positions": ["RB"]},
            [{"name": "Starter", "depth_chart_order": 1},
             {"name": "Middle", "depth_chart_order": 2}])
        assert "Starter" in got and "Middle" not in got

    def test_an_unknown_depth_chart_is_empty_not_invented(self):
        """A quarter of rostered skill players have no order."""
        assert notes.role_note({"positions": ["RB"]}, []) == ""


class TestNotesFor:
    def test_it_omits_the_notes_it_cannot_make(self):
        got = notes.notes_for({"positions": ["WR"]}, seasons=[], teammates=[])
        assert got == {}

    def test_a_full_picture_comes_through(self):
        got = notes.notes_for(
            {"injury_status": "PUP", "injury_body_part": "Knee - ACL",
             "age": 24, "years_exp": 2, "depth_chart_order": 2,
             "positions": ["RB"]},
            seasons=[(2024, 8.0), (2025, 12.0)],
            teammates=[{"name": "The Starter", "depth_chart_order": 1}],
            now=NOW)
        assert "Knee - ACL" in got["injury"]
        assert got["severity"] == "cannot practise yet"
        assert "trending up" in got["trajectory"]
        assert "The Starter" in got["role"]


class TestFormatting:
    def test_no_stray_space_before_the_semicolon(self):
        """" ".join put one there: "no detail given ; last news yesterday"."""
        got = notes.injury_note(
            {"injury_status": "PUP", "injury_body_part": "Achilles",
             "news_updated": int((NOW - 5 * 86400) * 1000)}, now=NOW)
        assert " ;" not in got
        assert "Achilles; last news" in got
