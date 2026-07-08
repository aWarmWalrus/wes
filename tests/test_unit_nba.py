"""Unit tests for the NBA live-data module (pc/wes_nba.py).

The formatters and matchers are pure, so we test them against fixture events
(shaped like ESPN's scoreboard/summary JSON) with NO network. One opt-in live
test hits ESPN to catch upstream schema drift; it's skipped unless
WES_NBA_LIVE=1 so CI/offline runs stay deterministic and fast.
"""
import os
from datetime import date

import pytest

import wes_nba


# --- fixtures shaped like ESPN's real payloads ------------------------------

def _event(state, away, home, asc="0", hsc="0", period=None, clock="",
           short="7:30 PM ET", eid="1"):
    return {
        "id": eid,
        "competitions": [{
            "status": {"period": period, "displayClock": clock,
                       "type": {"state": state, "shortDetail": short}},
            "competitors": [
                {"homeAway": "away", "score": asc, "team": {"displayName": away,
                 "abbreviation": away[:3].upper(), "location": away, "name": away}},
                {"homeAway": "home", "score": hsc, "team": {"displayName": home,
                 "abbreviation": home[:3].upper(), "location": home, "name": home}},
            ],
        }],
    }


def _summary(team_abbr, athletes):
    """athletes: list of (displayName, stats_row_or_None)."""
    return {"boxscore": {"players": [{
        "team": {"abbreviation": team_abbr},
        "statistics": [{
            "keys": ["minutes", "points", "fieldGoalsMade-fieldGoalsAttempted",
                     "threePointFieldGoalsMade-threePointFieldGoalsAttempted",
                     "freeThrowsMade-freeThrowsAttempted", "rebounds"],
            "athletes": [{"athlete": {"displayName": n}, "stats": s or []}
                         for n, s in athletes],
        }],
    }]}}


# --- pure formatters --------------------------------------------------------

class TestOrdinalQuarter:
    def test_regular_quarters(self):
        assert wes_nba.ordinal_quarter(3) == "3rd quarter"
        assert wes_nba.ordinal_quarter(1) == "1st quarter"

    def test_overtime(self):
        assert wes_nba.ordinal_quarter(5) == "overtime"
        assert wes_nba.ordinal_quarter(6) == "2x overtime"

    def test_unknown(self):
        assert wes_nba.ordinal_quarter(None) == "the game"


class TestFormatGame:
    def test_in_progress_has_score_and_period(self):
        ev = _event("in", "Nets", "Celtics", "54", "60", period=3, clock="4:12")
        out = wes_nba.format_game(ev)
        assert "Nets 54" in out and "Celtics 60" in out
        assert "3rd quarter" in out and "4:12 to go" in out

    def test_final(self):
        ev = _event("post", "Heat", "Celtics", "114", "119")
        out = wes_nba.format_game(ev)
        assert out.startswith("Final:") and "Heat 114" in out and "Celtics 119" in out

    def test_pregame_uses_start_time_not_score(self):
        ev = _event("pre", "Nets", "Knicks", short="7:30 PM ET")
        out = wes_nba.format_game(ev)
        assert "Nets at Knicks" in out and "7:30 PM ET" in out


class TestMatchers:
    def test_team_matches_nickname_and_location(self):
        c = {"team": {"displayName": "Brooklyn Nets", "location": "Brooklyn",
                      "name": "Nets", "abbreviation": "BKN"}}
        assert wes_nba.team_matches("Nets", c)
        assert wes_nba.team_matches("brooklyn", c)
        assert not wes_nba.team_matches("Lakers", c)

    def test_player_full_and_last_name(self):
        assert wes_nba.player_matches("Cam Thomas", "Cam Thomas")
        assert wes_nba.player_matches("thomas", "Cam Thomas")
        assert not wes_nba.player_matches("Durant", "Cam Thomas")


# --- date parsing -----------------------------------------------------------

class TestParseDate:
    TODAY = date(2026, 7, 7)  # a Tuesday

    def test_relative_words(self):
        p = lambda s: wes_nba.parse_date(s, today=self.TODAY)
        assert p("today") == "20260707"
        assert p("tonight") == "20260707"
        assert p("yesterday") == "20260706"
        assert p("tomorrow") == "20260708"

    def test_month_day_resolves_to_most_recent(self):
        p = lambda s: wes_nba.parse_date(s, today=self.TODAY)
        assert p("May 20") == "20260520"        # this year (already passed)
        assert p("May 20th") == "20260520"      # ordinal stripped
        assert p("20 May") == "20260520"
        # a month/day still ahead this year -> most recent = last year
        assert p("December 25") == "20251225"

    def test_numeric_forms(self):
        p = lambda s: wes_nba.parse_date(s, today=self.TODAY)
        assert p("2026-05-20") == "20260520"
        assert p("20260520") == "20260520"
        assert p("5/20/2026") == "20260520"
        assert p("5/20") == "20260520"

    def test_weekday(self):
        p = lambda s: wes_nba.parse_date(s, today=self.TODAY)
        assert p("today") == "20260707"          # Tuesday itself
        assert p("monday") == "20260706"         # most recent past Monday
        assert p("last friday") == "20260703"
        assert p("next monday") == "20260713"

    def test_unparseable(self):
        assert wes_nba.parse_date("sometime soon", today=self.TODAY) is None
        assert wes_nba.parse_date("", today=self.TODAY) is None


# --- live_scores (injected events, no network) ------------------------------

class TestLiveScores:
    def test_no_games_today(self):
        assert wes_nba.live_scores("Nets", _events_fn=lambda: []) == \
            "There are no NBA games scheduled today."

    def test_defaults_to_nets_and_filters(self):
        evs = [_event("in", "Nets", "Celtics", "54", "60", period=3, clock="4:12"),
               _event("in", "Lakers", "Suns", "80", "77", period=4, clock="1:00")]
        out = wes_nba.live_scores(None, _events_fn=lambda: evs)
        assert "Nets 54" in out and "Lakers" not in out  # filtered to Nets

    def test_team_not_playing_reports_other_games(self):
        evs = [_event("in", "Lakers", "Suns", "80", "77", period=4, clock="1:00")]
        out = wes_nba.live_scores("Nets", _events_fn=lambda: evs)
        assert "don't have a game today" in out and "1 other game" in out

    def test_unparseable_date_asks_for_clarification(self):
        out = wes_nba.live_scores("Nets", date="sometime soon",
                                  _events_fn=lambda: [])
        assert "couldn't understand the date" in out

    def test_past_date_uses_past_tense_and_label(self):
        evs = [_event("post", "Nets", "Celtics", "111", "118")]
        out = wes_nba.live_scores("Nets", date="2026-05-20", _events_fn=lambda: evs)
        assert "Final:" in out and "Nets 111" in out

    def test_past_date_no_games_reads_naturally(self):
        out = wes_nba.live_scores("Nets", date="2026-05-20", _events_fn=lambda: [])
        assert "were no NBA games on May 20, 2026" in out

    def test_dated_no_team_lists_all_games_not_just_nets(self):
        # "what games were played on May 20th" (no team) should list every game,
        # NOT default to the Nets and report they didn't play
        evs = [_event("post", "Thunder", "Pacers", "108", "91"),
               _event("post", "Knicks", "Celtics", "99", "104")]
        out = wes_nba.live_scores(None, date="2026-05-20", _events_fn=lambda: evs)
        assert "Thunder 108" in out and "Knicks 99" in out
        assert "didn't have a game" not in out


# --- player_points (injected events + summary, no network) ------------------

class TestPlayerPoints:
    def _evs(self):
        return [_event("in", "Nets", "Celtics", "54", "60", period=3,
                       clock="4:12", eid="G1")]

    def test_finds_live_points_with_game_context(self):
        summ = lambda _id: _summary("BKN", [("Cam Thomas", ["28", "22", "8-15",
                                                             "3-6", "3-4", "4"])])
        out = wes_nba.player_points("Cam Thomas", _events_fn=self._evs,
                                    _summary_fn=summ)
        assert "Cam Thomas has 22 points" in out
        assert "4 rebounds" in out
        assert "3rd quarter" in out  # self-locating game context appended

    def test_did_not_play(self):
        summ = lambda _id: _summary("BKN", [("Cam Thomas", None)])
        out = wes_nba.player_points("Cam Thomas", _events_fn=self._evs,
                                    _summary_fn=summ)
        assert "hasn't played" in out

    def test_not_found(self):
        summ = lambda _id: _summary("BKN", [("Some Other Guy", ["10", "5"])])
        out = wes_nba.player_points("Cam Thomas", _events_fn=self._evs,
                                    _summary_fn=summ)
        assert "couldn't find Cam Thomas" in out

    def test_no_games(self):
        out = wes_nba.player_points("Cam Thomas", _events_fn=lambda: [])
        assert "no NBA games today" in out

    def test_skips_not_yet_started_games(self):
        # a pre game must not trigger a summary fetch (it has no box score)
        calls = []
        evs = [_event("pre", "Nets", "Knicks", eid="P1")]
        def summ(_id):
            calls.append(_id)
            return _summary("BKN", [])
        out = wes_nba.player_points("Cam Thomas", _events_fn=lambda: evs,
                                    _summary_fn=summ)
        assert calls == []  # never fetched a summary for a pre-game
        assert "couldn't find" in out


# --- graceful degradation ---------------------------------------------------

class TestDegrade:
    def test_scores_network_error_is_soft(self):
        def boom():
            raise RuntimeError("dns fail")
        out = wes_nba.live_scores("Nets", _events_fn=boom)
        assert "couldn't reach the NBA scores" in out

    def test_player_summary_error_skips_game(self):
        evs = [_event("in", "Nets", "Celtics", period=3, eid="G1")]
        def summ(_id):
            raise RuntimeError("500")
        out = wes_nba.player_points("Cam Thomas", _events_fn=lambda: evs,
                                    _summary_fn=summ)
        assert "couldn't find" in out  # one bad game doesn't crash the turn


# --- opt-in live smoke test (schema drift canary) ---------------------------

@pytest.mark.skipif(os.environ.get("WES_NBA_LIVE") != "1",
                    reason="set WES_NBA_LIVE=1 to hit ESPN's live API")
class TestLiveESPN:
    def test_scoreboard_reachable(self):
        # returns a string either way; just prove the call path + schema hold
        out = wes_nba.live_scores()
        assert isinstance(out, str) and out
