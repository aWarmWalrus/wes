"""Unit tests for the Sleeper adapter (wes_sleeper, #039).

Network-free: every parser is pure and takes an already-fetched payload, and
the few fetchers are exercised with injected `_get_fn`. Payload shapes are
copied from real recon against the owner's league (1393935116232818688) on
2026-08-14, not invented.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "pc"))

import wes_nfl  # noqa: E402
import wes_sleeper as sl  # noqa: E402


class TestParseScoring:
    """Sleeper ships scoring as structured JSON, so this is a lookup table
    rather than a parser — but a silent key mismatch would misprice every
    player in the league, so the mapping is pinned."""

    REAL = {"pass_yd": 0.04, "pass_td": 4.0, "pass_int": -1.0,
            "rush_yd": 0.1, "rush_td": 6.0,
            "rec": 0.5, "rec_yd": 0.1, "rec_td": 6.0,
            "fum_lost": -2.0, "sack": 1.0, "int": 2.0, "fum_rec": 2.0,
            "fgm_40_49": 4.0, "fgm_50_59": 5.0, "xpm": 1.0, "xpmiss": -1.0,
            "pts_allow_0": 10.0, "pts_allow_1_6": 7.0, "pts_allow_7_13": 4.0,
            "pts_allow_14_20": 1.0, "pts_allow_21_27": 0.0,
            "pts_allow_28_34": -1.0, "pts_allow_35p": -4.0}

    def test_maps_sleeper_keys_to_our_canonical_ones(self):
        w = sl.parse_scoring(self.REAL)["weights"]
        assert w["PassYds"] == 0.04
        assert w["RecTD"] == 6.0
        assert w["Rec"] == 0.5          # this league is half-PPR
        assert w["Int"] == -1.0
        assert w["DefInt"] == 2.0       # sleeper's bare "int" is DEFENSIVE
        assert w["FG50"] == 5.0

    def test_defensive_int_and_thrown_int_do_not_collide(self):
        """`pass_int` (thrown, negative) and `int` (caught, positive) are
        different facts. Mapping both onto one key would make every QB and
        every defence wrong at once."""
        w = sl.parse_scoring({"pass_int": -2.0, "int": 3.0})["weights"]
        assert w["Int"] == -2.0 and w["DefInt"] == 3.0

    def test_points_allowed_becomes_our_ordered_ladder(self):
        tiers = sl.parse_scoring(self.REAL)["tiers"]
        assert tiers[0] == (0, 10.0)
        assert tiers[-1][1] == -4.0
        caps = [c for c, _ in tiers]
        assert caps == sorted(caps)     # ascending, or lookup picks wrong band

    def test_a_league_without_points_allowed_keeps_our_default_ladder(self):
        """An empty ladder would value every defence identically at zero, which
        is worse than a sane default."""
        tiers = sl.parse_scoring({"rec": 1.0})["tiers"]
        assert tiers == list(wes_nfl.POINTS_ALLOWED_TIERS)

    def test_unmodelled_settings_are_reported_not_swallowed(self):
        """A scoring rule we silently drop is a systematic mispricing. It must
        be visible rather than inferred later from bad advice."""
        out = sl.parse_scoring({"rec": 1.0, "some_new_stat": 3.0})
        assert "some_new_stat" in out["unknown"]

    def test_known_but_unmodelled_families_are_not_noise(self):
        """IDP and bonus settings are deliberately unmodelled; listing them as
        'unknown' every run would train us to ignore the field."""
        out = sl.parse_scoring({"idp_tkl": 1.0, "bonus_rec_te": 0.5})
        assert out["unknown"] == []

    def test_two_point_flavours_collapse_to_one_key(self):
        w = sl.parse_scoring({"pass_2pt": 2.0, "rush_2pt": 2.0,
                              "rec_2pt": 2.0})["weights"]
        assert w["2PT"] == 2.0

    def test_junk_values_are_ignored_rather_than_crashing(self):
        w = sl.parse_scoring({"rec": "not a number", "rec_td": 6.0})["weights"]
        assert w["RecTD"] == 6.0

    def test_output_shape_matches_the_yahoo_parser_contract(self):
        out = sl.parse_scoring(self.REAL)
        assert set(out) == {"weights", "tiers", "unknown"}
        # The whole point of an adapter: the valuer can't tell the platforms
        # apart, so a real stat line must score without any special-casing.
        pts = wes_nfl.fantasy_points({"RecYds": 100, "RecTD": 1, "Rec": 5},
                                     out["weights"], out["tiers"])
        assert pts == 100 * 0.1 + 6.0 + 5 * 0.5


class TestParseRosterSlots:
    def test_flex_maps_to_the_optimizers_spelling(self):
        assert sl.parse_roster_slots(["QB", "FLEX", "BN"]) == ["QB", "W/R/T", "BN"]

    def test_superflex_and_def_aliases(self):
        assert sl.parse_roster_slots(["SUPER_FLEX", "DST"]) == ["Q/W/R/T", "DEF"]

    def test_order_is_preserved(self):
        """Sleeper aligns `starters` POSITIONALLY to this list, so reordering
        would assign every starter the wrong slot."""
        real = ["QB", "RB", "RB", "WR", "WR", "TE", "FLEX", "FLEX", "K", "DEF"]
        assert sl.parse_roster_slots(real)[:3] == ["QB", "RB", "RB"]
        assert sl.parse_roster_slots(real)[6] == "W/R/T"

    def test_an_unknown_slot_passes_through_rather_than_vanishing(self):
        """A slot we can't name is still a slot that must be filled; dropping
        it would let the optimizer field an illegal lineup."""
        assert sl.parse_roster_slots(["QB", "WEIRD"]) == ["QB", "WEIRD"]


class TestParsePlayers:
    RAW = {
        "4034": {"full_name": "Christian McCaffrey", "position": "RB",
                 "team": "SF", "espn_id": 3117251, "gsis_id": "00-0033280",
                 "yahoo_id": 30121, "injury_status": None},
        "PIT": {"full_name": None, "position": "DEF", "team": "PIT"},
        "junk": {"position": "WR"},            # no name at all
        "bad": "not a dict",
    }

    def test_keeps_the_join_ids(self):
        """espn_id/gsis_id are why this integration is cheap: a Sleeper player
        joins to our ESPN valuations BY ID instead of by fuzzy name match."""
        idx = sl.parse_players(self.RAW)
        assert idx["4034"]["espn_id"] == "3117251"
        assert idx["4034"]["gsis_id"] == "00-0033280"

    def test_team_defences_get_a_usable_name(self):
        """Sleeper gives a DEF no person-name and keys it by team abbreviation;
        without this the DEF slot would be an unnamed player."""
        assert sl.parse_players(self.RAW)["PIT"]["name"] == "PIT"

    def test_nameless_and_malformed_entries_are_dropped(self):
        idx = sl.parse_players(self.RAW)
        assert "junk" not in idx and "bad" not in idx

    def test_ids_are_strings_so_lookups_cannot_miss_on_type(self):
        idx = sl.parse_players(self.RAW)
        assert isinstance(idx["4034"]["espn_id"], str)


class TestParseRoster:
    INDEX = {
        "1": {"name": "QB One", "positions": ["QB"], "team": "PHI",
              "espn_id": "1", "gsis_id": None, "status": ""},
        "2": {"name": "RB Two", "positions": ["RB"], "team": "SF",
              "espn_id": "2", "gsis_id": None, "status": "Questionable"},
        "9": {"name": "Bench Guy", "positions": ["WR"], "team": "NYJ",
              "espn_id": "9", "gsis_id": None, "status": ""},
    }
    SLOTS = ["QB", "RB", "WR"]

    def test_slot_comes_from_POSITION_IN_starters(self):
        """Sleeper does not store a slot on the player — it is implied by the
        index in `starters`, aligned to roster_positions."""
        out = sl.parse_roster({"starters": ["1", "2"], "players": ["1", "2"]},
                              self.INDEX, self.SLOTS)
        by_name = {p["name"]: p["slot"] for p in out}
        assert by_name == {"QB One": "QB", "RB Two": "RB"}

    def test_zero_means_an_empty_slot_not_a_player(self):
        """An unfilled starting slot is the literal string '0'. Treating it as
        a player id would invent a roster member."""
        out = sl.parse_roster({"starters": ["1", "0"], "players": ["1"]},
                              self.INDEX, self.SLOTS)
        assert [p["name"] for p in out] == ["QB One"]

    def test_players_not_in_starters_are_benched(self):
        out = sl.parse_roster(
            {"starters": ["1"], "players": ["1", "9"]}, self.INDEX, self.SLOTS)
        assert [(p["name"], p["slot"]) for p in out] == [
            ("QB One", "QB"), ("Bench Guy", "BN")]

    def test_nobody_is_listed_twice(self):
        out = sl.parse_roster({"starters": ["1"], "players": ["1"]},
                              self.INDEX, self.SLOTS)
        assert len(out) == 1

    def test_an_unknown_id_still_yields_a_row(self):
        """Better a placeholder than a silently shorter roster — a missing
        player would make the optimizer think a slot was free."""
        out = sl.parse_roster({"starters": ["404"], "players": ["404"]},
                              self.INDEX, self.SLOTS)
        assert len(out) == 1 and "404" in out[0]["name"]

    def test_injury_status_is_carried(self):
        out = sl.parse_roster({"starters": ["2"], "players": ["2"]},
                              self.INDEX, ["RB"])
        assert out[0]["status"] == "Questionable"


class TestFreeAgents:
    IDX = {
        "1": {"name": "Owned", "positions": ["RB"], "team": "SF",
              "espn_id": None, "gsis_id": None, "status": ""},
        "2": {"name": "Available", "positions": ["WR"], "team": "NYJ",
              "espn_id": None, "gsis_id": None, "status": ""},
        "3": {"name": "A Linebacker", "positions": ["LB"], "team": "NYJ",
              "espn_id": None, "gsis_id": None, "status": ""},
    }

    def _get(self, url, **kw):
        return [{"roster_id": 1, "players": ["1"]}]

    def test_availability_is_the_complement_of_every_roster(self):
        """Sleeper has no free-agent endpoint; availability is DERIVED from all
        rosters, so a roster read that silently failed would make the whole
        league look available."""
        out = sl.free_agents("L", _get_fn=self._get, _index_fn=lambda: self.IDX)
        assert [p["name"] for p in out] == ["Available"]

    def test_positions_outside_the_league_are_excluded(self):
        out = sl.free_agents("L", _get_fn=self._get, _index_fn=lambda: self.IDX)
        assert "A Linebacker" not in [p["name"] for p in out]

    def test_unreachable_sleeper_degrades_to_a_string(self):
        def boom(url, **kw):
            raise OSError("network down")
        out = sl.free_agents("L", _get_fn=boom, _index_fn=lambda: self.IDX)
        assert isinstance(out, str) and "couldn't reach" in out


class TestRosterPlayers:
    def test_missing_roster_degrades_to_a_string(self):
        """Matches wes_yahoo.roster_players: callers relay a degradation
        verbatim, so an adapter that raised where the other returned would
        break them."""
        out = sl.roster_players("L", 99, _get_fn=lambda u, **k: [
            {"roster_id": 1, "players": [], "starters": []}],
            _index_fn=lambda: {})
        assert isinstance(out, str) and "No roster 99" in out

    def test_unreachable_sleeper_degrades_to_a_string(self):
        def boom(url, **kw):
            raise OSError("nope")
        out = sl.roster_players("L", 1, _get_fn=boom, _index_fn=lambda: {})
        assert isinstance(out, str) and "couldn't reach" in out


class TestFindRosterId:
    def _get(self, url, **kw):
        if url.endswith("/users"):
            return [{"user_id": "u1", "display_name": "awarmwalrus"},
                    {"user_id": "u2", "display_name": "someone"}]
        return [{"roster_id": 3, "owner_id": "u1"},
                {"roster_id": 7, "owner_id": "u2"}]

    def test_resolves_display_name_to_roster_id(self):
        assert sl.find_roster_id("L", "awarmwalrus", _get_fn=self._get) == 3

    def test_is_case_insensitive(self):
        assert sl.find_roster_id("L", "AWarmWalrus", _get_fn=self._get) == 3

    def test_unknown_user_is_none_not_a_wrong_roster(self):
        assert sl.find_roster_id("L", "nobody", _get_fn=self._get) is None


class TestScoringGapsFoundLive:
    """Settings the `unknown` list surfaced on the first real run against the
    owner's league (2026-08-14) — proof the honesty of that field earns its
    keep."""

    def test_60_yard_field_goals_are_scored_not_dropped(self):
        """`fgm_60p` was unmapped on first contact. A 60+ yarder is still a
        made 50+ FG in every feed we have, so it folds into that bucket;
        dropping it would undervalue a big-leg kicker."""
        out = sl.parse_scoring({"fgm_60p": 6.0})
        assert out["weights"]["FG50"] == 6.0
        assert "fgm_60p" not in out["unknown"]

    def test_forced_fumbles_stay_honestly_unknown(self):
        """`ff` is real and we have no forced-fumble stat to score it with.
        Silently ignoring it would hide a known mispricing; it stays visible
        until there's data behind it."""
        assert "ff" in sl.parse_scoring({"ff": 1.0})["unknown"]


class TestLoginWallDetection:
    """Sleeper bounces a signed-out request to a marketing page instead of
    erroring, so a logged-out scrape returns a valid page about something else.
    Detecting that explicitly is what stops it looking like an empty roster."""

    def test_redirect_url_is_a_login_wall(self):
        assert sl._is_login_wall(
            "https://sleeper.com/?redirect=%2Fleagues%2F1%2Fteam&login=", "")

    def test_login_text_is_a_login_wall(self):
        assert sl._is_login_wall("https://sleeper.com/leagues/1/team",
                                 "SCORES FANTASY LOG IN SIGN UP")

    def test_a_real_team_page_is_not(self):
        assert not sl._is_login_wall("https://sleeper.com/leagues/1/team",
                                     "My Team QB Josh Allen BN")

    def test_logged_in_degrades_rather_than_raising(self):
        """This is the check run to diagnose another failure, so it has to
        survive the failure it is diagnosing."""
        class Boom:
            def __enter__(self):
                raise RuntimeError("no browser")

            def __exit__(self, *a):
                return False
        ok, detail = sl.logged_in("L", _session_cls=Boom)
        assert ok is False and "couldn't check" in detail
