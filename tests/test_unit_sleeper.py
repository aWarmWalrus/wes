"""Unit tests for the Sleeper adapter (wes_sleeper, #039).

Network-free: every parser is pure and takes an already-fetched payload, and
the few fetchers are exercised with injected `_get_fn`. Payload shapes are
copied from real recon against the owner's league (1393935116232818688) on
2026-08-14, not invented.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "pc"))

import json  # noqa: E402
import wes_draft_agent as agent  # noqa: E402
import wes_nfl  # noqa: E402
import wes_snapshot as snapshot  # noqa: E402
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

    def test_logged_in_degrades_rather_than_raising(self, monkeypatch):
        """This is the check run to diagnose another failure, so it has to
        survive the failure it is diagnosing. A token is set here so the check
        gets past the missing-token guard and actually reaches the browser."""
        monkeypatch.setattr(sl, "TOKEN", "tok")

        class Boom:
            def __enter__(self):
                raise RuntimeError("no browser")

            def __exit__(self, *a):
                return False
        ok, detail = sl.logged_in("L", _session_cls=Boom)
        assert ok is False and "couldn't check" in detail


class TestTokenAuthentication:
    """Token injection instead of an interactive login (2026-08-14).

    sleeper.com's login form sits behind hCaptcha, which is built to detect the
    browser Playwright launches — the owner could not get through it by hand,
    and one success would not last since hCaptcha re-challenges. But the captcha
    guards OBTAINING a session, not PRESENTING one."""

    class FakePage:
        def __init__(self):
            self.store = {}
            self.goto_urls = []

        def goto(self, url, **kw):
            self.goto_urls.append(url)

        def wait_for_timeout(self, _ms):
            pass

        def evaluate(self, _js, arg=None):
            if isinstance(arg, list) and len(arg) == 2:
                self.store[arg[0]] = arg[1]

    def test_writes_the_token_to_the_pinned_key(self, monkeypatch):
        monkeypatch.setattr(sl, "TOKEN", "abc123")
        page = self.FakePage()
        assert sl.authenticate(page) is True
        assert page.store == {"token": "abc123"}

    def test_loads_the_origin_first(self, monkeypatch):
        """localStorage is per-ORIGIN; writing it from about:blank lands
        nowhere at all, silently."""
        monkeypatch.setattr(sl, "TOKEN", "abc123")
        page = self.FakePage()
        sl.authenticate(page)
        assert page.goto_urls and page.goto_urls[0].startswith(sl.WEB)

    def test_no_token_configured_is_reported_not_silently_anonymous(
            self, monkeypatch):
        """Presenting an anonymous browser would scrape the marketing page and
        report a mysteriously empty roster."""
        monkeypatch.setattr(sl, "TOKEN", "")
        assert sl.authenticate(self.FakePage()) is False

    def test_logged_in_says_so_when_the_token_is_missing(self, monkeypatch):
        monkeypatch.setattr(sl, "TOKEN", "")
        ok, detail = sl.logged_in("L")
        assert ok is False and "WES_SLEEPER_TOKEN" in detail

    def test_the_pinned_key_is_the_one_that_was_verified(self):
        """Pinned by testing candidates one at a time against a cleared store:
        of token/user_token/auth_token/jwt/access_token, only this one got past
        the login wall."""
        assert sl.TOKEN_KEY == "token"


class TestDraftPickIdentity:
    """Found by reading a REAL pick record from a live mock draft
    (2026-08-15), not by testing: mock drafts return `roster_id: null` on every
    pick, because there is no league behind them. Keying "my picks" on roster_id
    therefore matched nothing, and the positional-need bump was computed against
    an empty roster while looking perfectly healthy."""

    REAL_MOCK_PICK = {
        "draft_id": "1394249532753080320", "draft_slot": 1, "pick_no": 1,
        "round": 1, "player_id": "9509", "picked_by": "", "roster_id": None,
        "metadata": {"first_name": "Bijan", "last_name": "Robinson",
                     "position": "RB", "team": "ATL"},
    }

    def test_a_real_mock_pick_has_no_roster_id(self):
        """Pinning the payload shape that broke the assumption."""
        assert self.REAL_MOCK_PICK["roster_id"] is None
        assert self.REAL_MOCK_PICK["draft_slot"] == 1

    def test_drafted_ids_come_from_player_id_not_names(self):
        """Sleeper hands us the exact id; name matching is how a draft bot
        recommends someone who is already gone."""
        picks = [self.REAL_MOCK_PICK,
                 {"player_id": "4034", "draft_slot": 2, "roster_id": None}]
        got = sl.drafted_player_ids(
            "D", _get_fn=lambda url, **kw: picks)
        assert got == {"9509", "4034"}

    def test_a_pick_with_no_player_id_is_skipped(self):
        got = sl.drafted_player_ids(
            "D", _get_fn=lambda url, **kw: [{"draft_slot": 3}])
        assert got == set()

    def test_unreachable_draft_api_yields_no_false_availability(self):
        """Returning {} on failure would make every drafted player look
        available — better an empty set than a confident wrong board."""
        def boom(url, **kw):
            raise OSError("down")
        try:
            got = sl.drafted_player_ids("D", _get_fn=boom)
        except OSError:
            got = None
        assert got in (set(), None)


class TestByeWeeks:
    """Bye weeks come from the SCHEDULE — no fantasy platform we read supplies
    one. Sleeper's player object has 40-odd fields and none is the bye."""

    ROWS = [
        {"season": "2026", "game_type": "REG", "week": "1",
         "home_team": "SF", "away_team": "SEA"},
        {"season": "2026", "game_type": "REG", "week": "2",
         "home_team": "SEA", "away_team": "SF"},
        {"season": "2026", "game_type": "REG", "week": "3",
         "home_team": "SF", "away_team": "SEA"},
        # SEA sits out week 4; SF plays someone else.
        {"season": "2026", "game_type": "REG", "week": "4",
         "home_team": "SF", "away_team": "KC"},
    ]

    def _byes(self, weeks=range(1, 5)):
        import wes_schedule
        return wes_schedule.parse_byes(self.ROWS, "2026", weeks=weeks)

    def test_the_missing_week_is_the_bye(self):
        assert self._byes()["SEA"] == 4

    def test_a_team_playing_every_week_has_no_bye_entry(self):
        """Omitted rather than guessed. Fantasy code treats a missing bye as
        UNKNOWN; inventing week 0 would read as 'never on bye'."""
        assert "SF" not in self._byes()

    def test_only_regular_season_games_count(self):
        """Preseason and playoffs would both invent phantom byes for teams
        simply not playing then."""
        import wes_schedule
        rows = self.ROWS + [{"season": "2026", "game_type": "POST",
                             "week": "4", "home_team": "SEA",
                             "away_team": "KC"}]
        assert wes_schedule.parse_byes(rows, "2026", weeks=range(1, 5))["SEA"] == 4

    def test_other_seasons_are_ignored(self):
        import wes_schedule
        rows = self.ROWS + [{"season": "2025", "game_type": "REG", "week": "4",
                             "home_team": "SEA", "away_team": "KC"}]
        assert wes_schedule.parse_byes(rows, "2026", weeks=range(1, 5))["SEA"] == 4

    def test_malformed_weeks_do_not_crash(self):
        import wes_schedule
        rows = self.ROWS + [{"season": "2026", "game_type": "REG",
                             "week": "N/A", "home_team": "SEA",
                             "away_team": "KC"}]
        assert wes_schedule.parse_byes(rows, "2026", weeks=range(1, 5))["SEA"] == 4


class TestSubmitPick:
    """The draft-room write (#039). Every row has its own `.draft-button`, and
    the DOM carries NO player id — so the click is matched by NAME and then
    verified by ID. Same discipline as the Yahoo executor: never assume a click
    did what it looked like."""

    class FakeBtn:
        def __init__(self, cls=""):
            self.cls = cls
            self.clicked = False

        def get_attribute(self, _n):
            return self.cls

        def scroll_into_view_if_needed(self):
            pass

        def click(self):
            self.clicked = True

    class FakeRow:
        def __init__(self, name, btn):
            self.name, self.btn = name, btn

        def inner_text(self):
            # The scroller reads the first row's text to tell "still moving"
            # from "bottomed out".
            return self.name

        def query_selector(self, sel):
            if "name-wrapper" in sel:
                return type("N", (), {"inner_text": lambda _s: self.name})()
            return self.btn

    class FakePage:
        def __init__(self, rows):
            self.rows = rows

        def goto(self, *a, **k):
            pass

        def wait_for_timeout(self, _ms):
            pass

        def evaluate(self, *a, **k):
            return True

        def query_selector_all(self, _sel):
            return self.rows

        def query_selector(self, _sel):
            return None

    def _session(self, page):
        class S:
            def __enter__(_s):
                return page

            def __exit__(_s, *a):
                return False
        return S

    def test_refuses_when_the_player_is_not_listed(self, monkeypatch):
        """Refusing beats clicking a different row — that is the Yahoo swap bug
        wearing a different hat."""
        monkeypatch.setattr(sl, "LIVE_WRITES_OK", lambda: True)
        monkeypatch.setattr(sl, "authenticate", lambda p: True)
        page = self.FakePage([self.FakeRow("Someone Else", self.FakeBtn())])
        try:
            sl.submit_pick("D", "1", "Target Guy",
                           _session_cls=self._session(page))
            assert False, "should have refused"
        except RuntimeError as e:
            assert "not available in the draft room" in str(e)

    def test_a_disabled_button_is_waited_out_then_TRIED(self, monkeypatch):
        """This used to REFUSE, on the theory that `disable` means "not on the
        clock" and a click would report a pick that never happened.

        Both halves of that turned out to be wrong. The class is not a
        reliable signal -- with 37 picks made, pick 38 ours and autopick
        confirmed off, it still read disabled after twenty seconds and
        refusing forfeited the pick (2026-08-22). And a no-op click can no
        longer be mistaken for success, because verification requires the
        pick's draft_slot to be OURS."""
        monkeypatch.setattr(sl, "LIVE_WRITES_OK", lambda: True)
        monkeypatch.setattr(sl, "authenticate", lambda p: True)
        monkeypatch.setattr(sl, "ENABLE_WAIT_TRIES", 2)
        btn = self.FakeBtn("draft-button disable")
        page = self.FakePage([self.FakeRow("Target Guy", btn)])
        ok = sl.submit_pick("D", "1", "Target Guy",
                            _session_cls=self._session(page),
                            _picks_fn=lambda d: [{"player_id": "1",
                                                  "draft_slot": 4}],
                            _sleep_fn=lambda _s: None, slot=4)
        assert ok is True and btn.clicked, "must try rather than stand down"

    def test_verifies_the_pick_by_ID_not_by_the_click(self, monkeypatch):
        """The name matched and the click landed, but the id that actually got
        drafted is the only proof."""
        monkeypatch.setattr(sl, "LIVE_WRITES_OK", lambda: True)
        monkeypatch.setattr(sl, "authenticate", lambda p: True)
        btn = self.FakeBtn("draft-button")
        page = self.FakePage([self.FakeRow("Target Guy", btn)])
        ok = sl.submit_pick("D", "77", "Target Guy",
                            _session_cls=self._session(page),
                            _picks_fn=lambda d: [{"player_id": "77"}],
                            _sleep_fn=lambda _s: None)
        assert ok is True and btn.clicked

    def test_a_click_that_drafted_someone_else_raises(self, monkeypatch):
        """The whole reason verification exists: names are ambiguous, ids are
        not."""
        monkeypatch.setattr(sl, "LIVE_WRITES_OK", lambda: True)
        monkeypatch.setattr(sl, "authenticate", lambda p: True)
        page = self.FakePage([self.FakeRow("Target Guy", self.FakeBtn())])
        try:
            sl.submit_pick("D", "77", "Target Guy",
                           _session_cls=self._session(page),
                           _picks_fn=lambda d: [{"player_id": "99"}],
                           _sleep_fn=lambda _s: None)
            assert False, "should have raised"
        except RuntimeError as e:
            assert "never appeared in the draft's picks" in str(e)

    def test_writes_off_blocks_everything(self, monkeypatch):
        monkeypatch.setattr(sl, "LIVE_WRITES_OK", lambda: False)
        try:
            sl.submit_pick("D", "1", "X")
            assert False, "should have refused"
        except RuntimeError as e:
            assert "writes are off" in str(e)

    def test_name_normalisation_survives_real_punctuation(self):
        assert sl._norm_name("A.J. Brown") == sl._norm_name("AJ Brown")
        assert sl._norm_name("Ja'Marr Chase") == sl._norm_name("JaMarr Chase")
        assert sl._norm_name("Marvin Harrison Jr.") == \
            sl._norm_name("Marvin Harrison Jr")


class TestVerificationIsPolled:
    """Found live, 2026-08-15: the first version read the picks ONCE, ~3s after
    the click, and reported failure on a pick that had actually succeeded.

    A false "did it work?" is worse than a slow yes — it invites a human into a
    live draft to fix something that is not broken, and the obvious fix (pick
    again) drafts twice."""

    def _page_and_session(self):
        btn = TestSubmitPick.FakeBtn("draft-button")
        page = TestSubmitPick.FakePage([TestSubmitPick.FakeRow("Guy", btn)])

        class S:
            def __enter__(_s):
                return page

            def __exit__(_s, *a):
                return False
        return S

    def test_a_pick_that_appears_late_still_verifies(self, monkeypatch):
        """Sleeper takes a moment to commit; one eager read is not proof."""
        monkeypatch.setattr(sl, "LIVE_WRITES_OK", lambda: True)
        monkeypatch.setattr(sl, "authenticate", lambda p: True)
        calls = {"n": 0}

        def picks(_d):
            calls["n"] += 1
            return [{"player_id": "77"}] if calls["n"] >= 3 else []
        ok = sl.submit_pick("D", "77", "Guy",
                            _session_cls=self._page_and_session(),
                            _picks_fn=picks, _sleep_fn=lambda _s: None)
        assert ok is True and calls["n"] == 3

    def test_it_gives_up_rather_than_polling_forever(self, monkeypatch):
        monkeypatch.setattr(sl, "LIVE_WRITES_OK", lambda: True)
        monkeypatch.setattr(sl, "authenticate", lambda p: True)
        calls = {"n": 0}

        def picks(_d):
            calls["n"] += 1
            return []
        try:
            sl.submit_pick("D", "77", "Guy",
                           _session_cls=self._page_and_session(),
                           _picks_fn=picks, _sleep_fn=lambda _s: None)
            assert False, "should have raised"
        except RuntimeError:
            pass
        # Bounded, not infinite -- but generous. Six attempts gave up at ~15s,
        # which is almost exactly how long Sleeper takes to commit, so a pick
        # that HAD worked was reported failed and the loop stood down
        # (2026-08-22, measured at the moment of a live click).
        assert calls["n"] == 12


class TestProjectionJoin:
    """Measured 2026-08-15: joining Sleeper players to ESPN projections by
    espn_id ALONE reached 91 of 324 projections, and 13 of the top 25 projected
    players — Gibbs, Nacua, Bijan Robinson, Chase — could never be drafted,
    because Sleeper leaves espn_id null for a great many players.

    A skipped player is not ranked low, he is ABSENT. That is why the drafts
    looked plausible while the best players were simply missing."""

    def test_name_fallback_recovers_a_player_with_no_espn_id(self):
        index = {"9221": {"name": "Jahmyr Gibbs", "positions": ["RB"],
                          "team": "DET", "espn_id": None}}
        pool = [{"name": "Jahmyr Gibbs", "espn_id": "4429795",
                 "positions": ["RB"], "gp": 17,
                 "cats": {"RushYds": 1000.0, "RushTD": 10.0}}]
        out = sl.draft_candidates(
            "L", "D", 1, limit=5,
            _get_fn=self._draft_get, _index_fn=lambda: index,
            _pool_fn=lambda: pool)
        assert not isinstance(out, str)
        assert [c["name"] for c in out["candidates"]] == ["Jahmyr Gibbs"]

    def test_the_id_join_still_wins_when_present(self):
        """Exact beats fuzzy: a player WITH an id must join on it, not on a
        name that might belong to someone else."""
        index = {"1": {"name": "Displayed Differently", "positions": ["RB"],
                       "team": "SF", "espn_id": "999"}}
        pool = [{"name": "Real Name", "espn_id": "999", "positions": ["RB"],
                 "gp": 17, "cats": {"RushYds": 500.0}}]
        out = sl.draft_candidates(
            "L", "D", 1, limit=5, _get_fn=self._draft_get,
            _index_fn=lambda: index, _pool_fn=lambda: pool)
        assert out["candidates"][0]["value"] > 0

    def test_position_is_part_of_the_name_key(self):
        """A shared surname must not cross-join a receiver onto some other
        position's projection."""
        index = {"1": {"name": "Same Name", "positions": ["WR"], "team": "SF",
                       "espn_id": None}}
        pool = [{"name": "Same Name", "espn_id": "5", "positions": ["LB"],
                 "gp": 17, "cats": {"RecYds": 900.0}}]
        out = sl.draft_candidates(
            "L", "D", 1, limit=5, _get_fn=self._draft_get,
            _index_fn=lambda: index, _pool_fn=lambda: pool)
        # Nothing joinable, so the board DEGRADES with a message rather than
        # returning an empty list — the cross-join is refused, not silently
        # accepted, and the caller is told why.
        assert isinstance(out, str) and "couldn't value" in out

    @staticmethod
    def _draft_get(path, **kw):
        if path.endswith("/picks"):
            return []
        if "/league/" in path and path.endswith("/rosters"):
            return []
        if "/league/" in path:
            return {"scoring_settings": {"rec": 1.0},
                    "roster_positions": ["RB", "WR", "BN"], "season": "2026"}
        return {"settings": {"teams": 10, "rounds": 15},
                "slot_to_roster_id": {"1": 1}, "season": "2026"}


class _Mouse:
    """Enough of page.mouse for the scroll path."""

    def __init__(self, page):
        self.page = page

    def move(self, *a, **k):
        pass

    def wheel(self, _dx, _dy):
        self.page.scrolled += 1


class TestWindowedList:
    """Sleeper renders ~59 player rows at a time, ordered by ITS ranking. A
    player we rate highly can sit far outside that window and be perfectly
    available while simply not drawn — a kicker failed seven times in one draft
    for this reason, never having been taken (2026-08-15). The search box is how
    a human reaches him."""

    class Box:
        def __init__(self):
            self.query = None

        def click(self):
            pass

        def fill(self, text):
            self.query = text

    class Page:
        """Rows appear only after SCROLLING — the virtualised window, modelled.

        The search box is deliberately absent: it returns the wrong player on
        the real site, so nothing may depend on it again."""

        def __init__(self, box, row_factory, rows_after=1):
            self.box, self._rows = box, row_factory
            self.scrolled = 0
            self.rows_after = rows_after
            self.mouse = _Mouse(self)

        def goto(self, *a, **k):
            pass

        def wait_for_timeout(self, _ms):
            pass

        def wait_for_selector(self, *a, **k):
            return True

        def evaluate(self, *a, **k):
            return True

        def query_selector_all(self, _sel):
            return self._rows() if self.scrolled >= self.rows_after else []

        def query_selector(self, sel):
            if "ReactVirtualized" in sel:
                return _Grid()
            if sel == ".player-rank-item2":
                got = self.query_selector_all(sel)
                return got[0] if got else None
            return None

    def test_it_SCROLLS_before_declaring_a_player_unavailable(self, monkeypatch):
        monkeypatch.setattr(sl, "LIVE_WRITES_OK", lambda: True)
        monkeypatch.setattr(sl, "authenticate", lambda p: True)
        btn = TestSubmitPick.FakeBtn("draft-button")
        box = self.Box()
        page = self.Page(box,
                         lambda: [TestSubmitPick.FakeRow("Deep Guy", btn)])

        class S:
            def __enter__(_s):
                return page

            def __exit__(_s, *a):
                return False
        ok = sl.submit_pick("D", "42", "Deep Guy", _session_cls=S,
                            _picks_fn=lambda d: [{"player_id": "42"}],
                            _sleep_fn=lambda _s: None)
        assert ok is True
        assert page.scrolled > 0             # it actually scrolled
        assert btn.clicked

    def test_the_error_now_distinguishes_gone_from_merely_unrendered(self,
                                                                    monkeypatch):
        """Two very different causes wore the same message before, and only one
        of them means 'pick someone else'."""
        monkeypatch.setattr(sl, "LIVE_WRITES_OK", lambda: True)
        monkeypatch.setattr(sl, "authenticate", lambda p: True)
        box = self.Box()
        # The list RENDERS -- it just does not contain him. That is the case
        # that genuinely means "pick someone else"; an empty list is a
        # different error entirely.
        page = self.Page(
            box, lambda: [TestSubmitPick.FakeRow("Someone Else", None)])

        class S:
            def __enter__(_s):
                return page

            def __exit__(_s, *a):
                return False
        try:
            sl.submit_pick("D", "42", "Ghost", _session_cls=S)
            assert False, "should have refused"
        except RuntimeError as e:
            assert "even after searching" in str(e)


class TestTokenFallback:
    """A shell opened before WES_SLEEPER_TOKEN was set does not inherit it, and
    the failure is quiet: a mock draft stood down on every turn, let
    cpu_autopick take all fifteen picks, and printed a plausible roster that
    proved nothing (2026-08-15)."""

    def test_the_environment_wins_when_it_has_the_value(self, monkeypatch):
        monkeypatch.setenv("WES_SLEEPER_TOKEN", "from-env")
        assert sl._read_token() == "from-env"

    def test_it_falls_back_to_the_persisted_value_on_windows(self, monkeypatch):
        monkeypatch.delenv("WES_SLEEPER_TOKEN", raising=False)
        monkeypatch.setattr(sl.os, "name", "nt")
        import types
        fake = types.SimpleNamespace(
            HKEY_CURRENT_USER=0,
            OpenKey=lambda *a: __import__("contextlib").nullcontext(object()),
            QueryValueEx=lambda *a: ("from-registry", 1))
        monkeypatch.setitem(__import__("sys").modules, "winreg", fake)
        assert sl._read_token() == "from-registry"

    def test_a_missing_value_is_empty_not_an_exception(self, monkeypatch):
        """Callers already report 'no token' helpfully; this must not raise
        past them."""
        monkeypatch.delenv("WES_SLEEPER_TOKEN", raising=False)
        monkeypatch.setattr(sl.os, "name", "nt")
        import types

        def boom(*a):
            raise OSError("no such value")
        fake = types.SimpleNamespace(HKEY_CURRENT_USER=0, OpenKey=boom,
                                     QueryValueEx=boom)
        monkeypatch.setitem(__import__("sys").modules, "winreg", fake)
        assert sl._read_token() == ""


class TestUnrenderedIsNotAbsent:
    """An unrendered list and an absent player look identical to a query
    selector. Reading the first as the second cost a pick: Amon-Ra St. Brown was
    declared gone on the run's first page load and drafted by us one pick later
    (2026-08-15)."""

    class Page(TestWindowedList.Page):
        def wait_for_selector(self, _sel, timeout=None):
            raise TimeoutError("never appeared")

    def test_an_empty_list_says_so_instead_of_blaming_the_player(self,
                                                                monkeypatch):
        monkeypatch.setattr(sl, "LIVE_WRITES_OK", lambda: True)
        monkeypatch.setattr(sl, "authenticate", lambda p: True)
        page = self.Page(TestWindowedList.Box(), lambda: [])

        class S:
            def __enter__(_s):
                return page

            def __exit__(_s, *a):
                return False
        try:
            sl.submit_pick("D", "42", "Amon-Ra St. Brown", _session_cls=S)
            assert False, "should have refused"
        except RuntimeError as e:
            assert "never rendered" in str(e)
            # AND crucially not the phrase the loop substitutes on.
            assert "not available in the draft room" not in str(e)


class TestDefenceNaming:
    """Sleeper's UI renders "Houston Texans"; ESPN's stat feed says "Texans".
    Taking last_name alone made the two agree with each other and disagree with
    the draft room, so eight attempts to draft a defence were reported as "he is
    gone" and the mock finished with four tight ends (2026-08-15)."""

    RAW = {"HOU": {"position": "DEF", "first_name": "Houston",
                   "last_name": "Texans", "full_name": None, "team": "HOU"}}

    def test_the_index_uses_the_name_the_draft_room_shows(self):
        idx = sl.parse_players(self.RAW)
        assert idx["HOU"]["name"] == "Houston Texans"

    def test_a_defence_with_no_club_name_still_gets_one(self):
        idx = sl.parse_players({"NYJ": {"position": "DEF", "team": "NYJ"}})
        assert idx["NYJ"]["name"] == "NYJ"

    def test_valuation_still_joins_against_espns_nickname(self, monkeypatch):
        """The fix must not trade a click bug for a silently unvalued position:
        an unvalued player is dropped from the board entirely."""
        pool = [{"name": "Texans", "positions": ["DEF"], "team": "Texans",
                 "gp": 17.0, "cats": {"Sack": 30.0}}]
        out = sl.draft_candidates(
            "L", "D", 1, limit=5,
            _index_fn=lambda: sl.parse_players(self.RAW),
            _pool_fn=lambda: pool,
            _picks=[],
            _get_fn=lambda path, **k: TestDefenceNaming._league(path))
        assert not isinstance(out, str), out
        names = [c["name"] for c in out["candidates"]]
        assert "Houston Texans" in names

    @staticmethod
    def _league(path):
        # `_get` hands the FULL url to the injected fetcher, not the path.
        if "/league/" in path:
            return {"scoring_settings": {"pts_allow_0": 10},
                    "roster_positions": ["DEF", "BN"]}
        if "/picks" in path:
            return []
        if "/draft/" in path:
            return {"status": "drafting", "settings": {"teams": 2, "rounds": 2},
                    "slot_to_roster_id": {"1": 1, "2": 2}, "type": "snake"}
        return {}


class TestByeAndPhaseContext:
    """The prompt asked the model to "consider bye-week spread" while the
    shortlist carried no bye weeks, and gave it nothing to reason with once
    every starting slot was full — the last seven picks of a clean mock were all
    RBs, justified in the same sentence five times (2026-08-15)."""

    @staticmethod
    def _get(path):
        if "/league/" in path:
            return {"scoring_settings": {"rec": 1},
                    "roster_positions": ["RB", "BN"]}
        if "/picks" in path:
            return []
        if "/draft/" in path:
            return {"status": "drafting", "settings": {"teams": 2, "rounds": 2},
                    "slot_to_roster_id": {"1": 1, "2": 2}, "type": "snake"}
        return {}

    def _board(self, monkeypatch, roster_players=(), byes=None):
        monkeypatch.setattr(
            snapshot, "byes",
            lambda: {"KC": 10, "SF": 10, "BUF": 7} if byes is None else byes)
        idx = {"1": {"name": "A Back", "positions": ["RB"], "team": "KC"},
               "2": {"name": "B Back", "positions": ["RB"], "team": "BUF"}}
        idx.update({str(p): {"name": f"Have{p}", "positions": ["RB"],
                             "team": "SF"} for p in roster_players})
        pool = [{"name": n["name"], "positions": ["RB"], "team": n["team"],
                 "gp": 17.0, "cats": {"Rec": 50.0}} for n in idx.values()]
        return sl.draft_candidates(
            "L", "D", 1, limit=6, _index_fn=lambda: idx,
            _pool_fn=lambda: pool, _picks=[], _get_fn=lambda p, **k: self._get(p))

    def test_every_candidate_carries_its_own_bye(self, monkeypatch):
        out = self._board(monkeypatch)
        assert not isinstance(out, str), out
        byes = {c["name"]: c["bye"] for c in out["candidates"]}
        assert byes["A Back"] == 10 and byes["B Back"] == 7

    def test_an_unknown_bye_is_none_not_a_guessed_week(self, monkeypatch):
        """Defaulting to a week would spread a roster off a bye nobody has."""
        out = self._board(monkeypatch, byes={})
        assert all(c["bye"] is None for c in out["candidates"])

    def test_phase_is_starters_while_a_slot_is_empty(self, monkeypatch):
        out = self._board(monkeypatch)
        assert out["phase"] == "starters"
        assert out["still_unfilled"]

    def test_the_shortlist_shows_the_bye_to_the_model(self):
        seen = {}

        def fake_post(body):
            seen.update(json.loads(body))
            return '{"player_key": "1", "reason": "ok"}'
        cands = [{"player_key": "1", "name": "A", "positions": ["RB"],
                  "team": "KC", "vor": 1.0, "bye": 10}]
        pick, _reason, source = agent.choose(cands, context={"phase": "depth"},
                                             _post_fn=fake_post)
        assert source == "model"
        sent = json.loads(seen["messages"][1]["content"])
        assert sent["shortlist"][0]["bye_week"] == 10
        assert sent["context"]["phase"] == "depth"


class TestDraftDayPreflight:
    """Every check corresponds to a failure that has already happened once. The
    point is to fail them all at 12:30 rather than one of them at 13:00."""

    def _clean(self, monkeypatch):
        import sleeper_draft_day as day
        import wes_draft_agent
        monkeypatch.setattr(sl, "TOKEN", "tok" * 40)
        monkeypatch.setattr(day.wes_execute, "writes_enabled", lambda: True)
        monkeypatch.setattr(day.wes_sleeper, "league", lambda i: {
            "name": "L", "status": "pre_draft", "draft_id": "D"})
        monkeypatch.setattr(day.wes_sleeper, "find_roster_id", lambda i, u: 3)
        monkeypatch.setattr(day.wes_sleeper, "draft", lambda d: {
            "status": "pre_draft", "start_time": 1788552000000,
            "settings": {"teams": 12, "rounds": 15, "pick_timer": 600,
                         "cpu_autopick": 1},
            "slot_to_roster_id": {"1": 1, "3": 3}})
        monkeypatch.setattr(day.wes_snapshot, "age_seconds", lambda: 3600.0)
        monkeypatch.setattr(day.wes_snapshot, "describe",
                            lambda: "snapshot\n  players 1")
        monkeypatch.setattr(wes_draft_agent, "_ask_model",
                            lambda payload, **k: {"player_key": "1"})
        return day

    def test_a_clean_machine_passes(self, monkeypatch):
        day = self._clean(monkeypatch)
        ok, lines = day.preflight(_probe_browser=False)
        assert ok, "\n".join(lines)
        assert any("roster_id 3" in ln for ln in lines)

    def test_a_missing_token_fails_it(self, monkeypatch):
        day = self._clean(monkeypatch)
        monkeypatch.setattr(sl, "TOKEN", "")
        ok, lines = day.preflight(_probe_browser=False)
        assert not ok
        assert any("FAIL" in ln and "token" in ln for ln in lines)

    def test_disabled_writes_fail_it(self, monkeypatch):
        """Otherwise the loop logs WOULD-take all afternoon and clicks
        nothing."""
        day = self._clean(monkeypatch)
        monkeypatch.setattr(day.wes_execute, "writes_enabled", lambda: False)
        ok, _lines = day.preflight(_probe_browser=False)
        assert not ok

    def test_a_stale_snapshot_fails_it(self, monkeypatch):
        day = self._clean(monkeypatch)
        monkeypatch.setattr(day.wes_snapshot, "age_seconds",
                            lambda: 40 * 3600.0)
        ok, _lines = day.preflight(_probe_browser=False)
        assert not ok

    def test_a_dead_model_fails_it_rather_than_degrading_quietly(self,
                                                                monkeypatch):
        """A dead Ollama crashes nothing -- every pick just becomes the
        engine's sort and the draft looks entirely normal."""
        day = self._clean(monkeypatch)
        import wes_draft_agent
        monkeypatch.setattr(wes_draft_agent, "_ask_model",
                            lambda payload, **k: None)
        ok, lines = day.preflight(_probe_browser=False)
        assert not ok
        assert any("fall back to the engine" in ln for ln in lines)

    def test_an_unassigned_slot_is_not_a_failure(self, monkeypatch):
        """Sleeper publishes the order when the draft STARTS, so pre-draft that
        field is legitimately empty and must not read as broken."""
        day = self._clean(monkeypatch)
        monkeypatch.setattr(day.wes_sleeper, "draft", lambda d: {
            "status": "pre_draft", "start_time": 0, "settings": {},
            "slot_to_roster_id": None})
        ok, lines = day.preflight(_probe_browser=False)
        assert ok, "\n".join(lines)
        assert any("not yet assigned" in ln for ln in lines)

    def test_one_broken_check_does_not_hide_the_others(self, monkeypatch):
        """A pre-flight that dies on its first problem tells you about one
        thing when you wanted the list."""
        day = self._clean(monkeypatch)
        monkeypatch.setattr(sl, "TOKEN", "")
        monkeypatch.setattr(day.wes_snapshot, "age_seconds",
                            lambda: 40 * 3600.0)
        ok, lines = day.preflight(_probe_browser=False)
        assert not ok
        assert sum(1 for ln in lines if "FAIL" in ln) == 2


class TestUndraftable:
    """23 players sit on IR and 40 are Inactive, and every one of them was on
    the board at full projected value with nothing marking them. This league
    has no IR slot, so a player who cannot take the field burns a bench seat
    all season (2026-08-15)."""

    def test_ir_is_excluded_not_merely_penalised(self):
        assert sl._can_play({"injury_status": "IR"}) is False

    def test_out_and_suspended_too(self):
        for st in ("Out", "Suspended", "NA", "DNR"):
            assert sl._can_play({"injury_status": st}) is False, st

    def test_pup_is_NOT_excluded(self):
        """A deliberate reversal: excluding PUP silently deleted George Kittle,
        a top-12 TE, over what is frequently just a camp designation. The feed
        carries no return date to tell the two apart, so we cost him value and
        say why rather than decide on ambiguity."""
        assert sl._can_play({"injury_status": "PUP",
                             "roster_status": "Active"}) is True

    def test_inactive_and_practice_squad_are_excluded(self):
        assert sl._can_play({"roster_status": "Inactive"}) is False
        assert sl._can_play({"roster_status": "Practice Squad"}) is False

    def test_questionable_is_a_judgment_call_and_stays_on_the_board(self):
        """Deliberately NOT excluded -- 82 players carry it, and dropping them
        would gut the board over what is only a probability."""
        assert sl._can_play({"injury_status": "Questionable",
                             "roster_status": "Active"}) is True

    def test_a_defence_has_neither_field_and_is_still_draftable(self):
        """A DEF carries no injury or roster status; filtering on their absence
        would delete every defence from the board."""
        assert sl._can_play({"name": "Houston Texans"}) is True

    def test_an_injured_player_never_reaches_the_board(self, monkeypatch):
        idx = {"1": {"name": "Hurt Guy", "positions": ["RB"], "team": "KC",
                     "injury_status": "IR", "roster_status": "Active"},
               "2": {"name": "Fit Guy", "positions": ["RB"], "team": "SF",
                     "injury_status": "", "roster_status": "Active"}}
        pool = [{"name": n["name"], "positions": ["RB"], "team": n["team"],
                 "gp": 17.0, "cats": {"Rec": 50.0}} for n in idx.values()]
        out = sl.draft_candidates(
            "L", "D", 1, limit=6, _index_fn=lambda: idx,
            _pool_fn=lambda: pool, _picks=[],
            _get_fn=lambda path, **k: TestByeAndPhaseContext._get(path))
        assert not isinstance(out, str), out
        names = [c["name"] for c in out["candidates"]]
        assert names == ["Fit Guy"]


class TestMarketRank:
    """Sleeper's search_rank is the closest thing to a market price we get for
    free, and its sentinels are a trap: 359 players carry 999 and 9468 carry
    9999999, so reading them as ranks files a fifth of the pool at "rank 999"
    -- unpopular rather than unranked."""

    def test_a_real_rank_survives(self):
        assert sl._market_rank(1) == 1
        assert sl._market_rank(997) == 997

    def test_both_sentinels_become_unknown(self):
        assert sl._market_rank(999) is None
        assert sl._market_rank(9999999) is None

    def test_junk_is_unknown_rather_than_an_exception(self):
        for bad in (None, "12", 0, -3, 3.5):
            assert sl._market_rank(bad) is None, bad

    def test_the_index_carries_it(self):
        idx = sl.parse_players({"7": {"position": "RB", "full_name": "A B",
                                      "team": "KC", "search_rank": 42}})
        assert idx["7"]["search_rank"] == 42

    def test_the_shortlist_shows_market_rank_to_the_model(self):
        seen = {}

        def fake_post(body):
            seen.update(json.loads(body))
            return '{"player_key": "1", "reason": "ok"}'
        cands = [{"player_key": "1", "name": "A", "positions": ["RB"],
                  "team": "KC", "vor": 1.0, "market_rank": 12,
                  "injury": "Questionable"}]
        _pick, _why, source = agent.choose(cands, context={},
                                           _post_fn=fake_post)
        assert source == "model"
        sent = json.loads(seen["messages"][1]["content"])
        assert sent["shortlist"][0]["market_rank"] == 12
        assert sent["shortlist"][0]["injury"] == "Questionable"


class TestPositionalRuns:
    """The prompt has told the model to consider positional runs since the
    first version while handing it no picks to see one in -- the identical
    omission as the bye weeks, still live in a second place (2026-08-15)."""

    @staticmethod
    def _get(path):
        """A 12x15 draft, so a 39-pick history does not simply end it."""
        if "/league/" in path:
            return {"scoring_settings": {"rec": 1},
                    "roster_positions": ["RB", "BN"]}
        if "/picks" in path:
            return []
        if "/draft/" in path:
            return {"status": "drafting",
                    "settings": {"teams": 12, "rounds": 15},
                    "slot_to_roster_id": {str(i): i for i in range(1, 13)},
                    "type": "snake"}
        return {}

    def _board(self, picks):
        idx = {str(i): {"name": f"P{i}", "positions": ["RB"], "team": "KC"}
               for i in range(1, 4)}
        pool = [{"name": v["name"], "positions": ["RB"], "team": "KC",
                 "gp": 17.0, "cats": {"Rec": 50.0}} for v in idx.values()]
        return sl.draft_candidates(
            "L", "D", 1, limit=6, _index_fn=lambda: idx, _pool_fn=lambda: pool,
            _picks=picks,
            _get_fn=lambda path, **k: TestPositionalRuns._get(path))

    def test_it_counts_what_the_room_just_took(self):
        picks = [{"pick_no": i, "draft_slot": 2, "player_id": "99",
                  "metadata": {"position": "RB" if i % 2 else "WR"}}
                 for i in range(1, 9)]
        out = self._board(picks)
        assert not isinstance(out, str), out
        assert out["recent_picks_by_position"] == {"RB": 4, "WR": 4}

    def test_it_looks_back_only_one_trip_round_the_board(self):
        """A run is about what happens before your NEXT turn, so ancient picks
        must not dilute it."""
        old = [{"pick_no": i, "draft_slot": 2, "player_id": "99",
                "metadata": {"position": "QB"}} for i in range(1, 40)]
        new = [{"pick_no": 40 + i, "draft_slot": 2, "player_id": "99",
                "metadata": {"position": "RB"}} for i in range(sl.RUN_WINDOW)]
        out = self._board(old + new)
        assert out["recent_picks_by_position"] == {"RB": sl.RUN_WINDOW}


class TestInjuryPenalty:
    """PUP costs value and is NAMED. A hard exclusion would have deleted a
    top-12 tight end over a camp designation (2026-08-15)."""

    def _board(self, injury):
        idx = {"1": {"name": "Risky", "positions": ["RB"], "team": "KC",
                     "injury_status": injury, "roster_status": "Active"},
               "2": {"name": "Healthy", "positions": ["RB"], "team": "SF",
                     "injury_status": "", "roster_status": "Active"}}
        pool = [{"name": v["name"], "positions": ["RB"], "team": v["team"],
                 "gp": 17.0, "cats": {"Rec": 50.0}} for v in idx.values()]
        out = sl.draft_candidates(
            "L", "D", 1, limit=6, _index_fn=lambda: idx, _pool_fn=lambda: pool,
            _picks=[], _get_fn=lambda p, **k: TestPositionalRuns._get(p))
        return {c["name"]: c for c in out["candidates"]}

    def test_pup_stays_on_the_board_but_ranks_below_an_equal_healthy_player(self):
        got = self._board("PUP")
        assert "Risky" in got, "must not be excluded"
        assert got["Risky"]["adj_value"] < got["Healthy"]["adj_value"]

    def test_the_reason_says_which_designation(self):
        assert "listed PUP" in self._board("PUP")["Risky"]["fit_reasons"]

    def test_questionable_costs_far_less_than_pup(self):
        q = self._board("Questionable")["Risky"]["fit_penalty"]
        pup = self._board("PUP")["Risky"]["fit_penalty"]
        assert 0 < q < pup

    def test_a_healthy_player_pays_nothing(self):
        assert self._board("")["Healthy"]["fit_penalty"] == 0.0


class TestHandcuff:
    """A handcuff is the reserve behind a starter WE own -- he inherits the
    touches if our man goes down. The join is team + position + a strictly
    lower depth-chart order, and it is computed here rather than described to
    the model: a small model asked to do that in its head produces a confident
    wrong answer, which is what the false bye-week claims looked like."""

    MINE = [{"name": "Saquon Barkley", "team": "PHI", "positions": ["RB"],
             "depth_chart_order": 1}]

    def test_it_names_the_starter_we_hold(self):
        got = sl._handcuff_for(
            {"team": "PHI", "positions": ["RB"], "depth_chart_order": 2},
            self.MINE)
        assert got == "Saquon Barkley"

    def test_a_different_team_is_not_a_handcuff(self):
        assert sl._handcuff_for(
            {"team": "DAL", "positions": ["RB"], "depth_chart_order": 2},
            self.MINE) is None

    def test_a_different_position_is_not_a_handcuff(self):
        """A PHI receiver does not inherit a running back's carries."""
        assert sl._handcuff_for(
            {"team": "PHI", "positions": ["WR"], "depth_chart_order": 2},
            self.MINE) is None

    def test_a_player_AHEAD_of_ours_is_not_a_handcuff(self):
        """Direction matters: he would take OUR man's touches, not inherit
        them."""
        assert sl._handcuff_for(
            {"team": "PHI", "positions": ["RB"], "depth_chart_order": 1},
            [{"name": "Ours", "team": "PHI", "positions": ["RB"],
              "depth_chart_order": 2}]) is None

    def test_an_unknown_depth_order_yields_none_not_a_guess(self):
        """25% of rostered skill players have no depth chart order."""
        assert sl._handcuff_for(
            {"team": "PHI", "positions": ["RB"], "depth_chart_order": None},
            self.MINE) is None

    def test_it_names_the_highest_starter_when_several_are_ahead(self):
        mine = [{"name": "RB1", "team": "PHI", "positions": ["RB"],
                 "depth_chart_order": 1},
                {"name": "RB2", "team": "PHI", "positions": ["RB"],
                 "depth_chart_order": 2}]
        assert sl._handcuff_for(
            {"team": "PHI", "positions": ["RB"], "depth_chart_order": 3},
            mine) == "RB1"


class TestHeadlessDefault:
    """The browser must not steal focus on the owner's desktop. A Chrome popup
    per pick, fifteen times an afternoon, on the machine they are working on
    (2026-08-16)."""

    def test_headless_is_the_default(self):
        assert sl.HEADLESS is True

    def test_it_can_still_be_watched(self, monkeypatch):
        """Worth doing when the draft room's DOM changes under us."""
        monkeypatch.setenv("WES_SLEEPER_HEADLESS", "0")
        import importlib
        importlib.reload(sl)
        try:
            assert sl.HEADLESS is False
        finally:
            monkeypatch.delenv("WES_SLEEPER_HEADLESS")
            importlib.reload(sl)

    def test_a_session_honours_an_explicit_override(self):
        """Reads can run visible while a draft runs hidden, and vice versa."""
        assert sl._Session(headless=False).headless is False
        assert sl._Session(headless=True).headless is True


class TestSeatInAnyDraft:
    """A MOCK has no league at all (league_id is null), so the roster route
    cannot find our seat. draft_order -- keyed by user id -- is the only place
    it is written down, and reading it wrong is how an early loop sat on slot 1
    while our real seat was elsewhere and made zero picks (2026-08-15)."""

    def _get(self, path):
        if "/user/" in path:
            return {"user_id": "U1", "display_name": "awarmwalrus"}
        return {}

    def test_it_finds_our_slot(self):
        got = sl.slot_in_draft("D", "awarmwalrus", _get_fn=lambda p, **k: self._get(p),
                               _draft_fn=lambda d: {"draft_order": {"U1": 7}})
        assert got == 7

    def test_a_draft_we_have_not_joined_is_None_not_a_guess(self):
        """The seat is claimed on JOINING. No entry means no seat, and drafting
        into someone else's slot is not a recoverable mistake."""
        got = sl.slot_in_draft("D", "awarmwalrus", _get_fn=lambda p, **k: self._get(p),
                               _draft_fn=lambda d: {"draft_order": {"OTHER": 7}})
        assert got is None

    def test_an_empty_draft_order_is_None(self):
        """Pre-draft, Sleeper has published nothing yet."""
        got = sl.slot_in_draft("D", "awarmwalrus", _get_fn=lambda p, **k: self._get(p),
                               _draft_fn=lambda d: {"draft_order": None})
        assert got is None

    def test_an_unknown_username_is_None(self):
        got = sl.slot_in_draft("D", "nobody", _get_fn=lambda p, **k: {},
                               _draft_fn=lambda d: {"draft_order": {"U1": 7}})
        assert got is None

    def test_user_id_lookup(self):
        assert sl.user_id("awarmwalrus",
                          _get_fn=lambda p, **k: self._get(p)) == "U1"

    def test_user_id_of_nobody_is_None(self):
        assert sl.user_id("nobody", _get_fn=lambda p, **k: {}) is None


class TestJoinDraft:
    """Two real bugs, both found the hard way (2026-08-21).

    THE CLICK went to `.header-button`, the wrapper. The onclick is on its
    grandchild `.claim-text`. Eleven gestures across two investigations were
    all aimed at an element with no handler, and every null result sent me
    hunting a more exotic cause.

    THE VERIFICATION used `draft_order`, which lags the claim by over a
    minute -- so it reported failure on a seat we already held, and the first
    fix for that reintroduced double-claiming at the other end."""

    class Btn:
        def __init__(self):
            self.clicked = False

        def click(self):
            self.clicked = True

    class Seat:
        """A seat whose handler lives on .claim-text, like the real one."""

        def __init__(self, label, claim=None, wrapper=None):
            self.label = label
            self._claim = claim
            self._wrapper = wrapper

        def inner_text(self):
            return self.label

        def query_selector(self, sel):
            if sel == ".claim-text":
                return self._claim
            if sel == ".header-button":
                return self._wrapper
            return None

    class Page:
        def __init__(self, seats):
            self.seats = seats

        def goto(self, *a, **k):
            pass

        def wait_for_timeout(self, _ms):
            pass

        def query_selector_all(self, _sel):
            return self.seats

    def _session(self, page):
        class S:
            def __enter__(_s):
                return page

            def __exit__(_s, *a):
                return False
        return S

    def test_it_clicks_the_element_that_has_the_handler(self, monkeypatch):
        """.header-button has no onclick; clicking it is why this never
        worked."""
        monkeypatch.setattr(sl, "LIVE_WRITES_OK", lambda: True)
        monkeypatch.setattr(sl, "authenticate", lambda p: True)
        claim, wrapper = self.Btn(), self.Btn()
        seats = [self.Seat("Someone", None, None), self.Seat("Someone"),
                 self.Seat("CLAIM Team 2", claim, wrapper),
                 self.Seat("CLAIM Team 2", claim, wrapper)]

        def after_click():
            seats[2].label = seats[3].label = "awarmwalrus"
        claim.click = lambda: (setattr(claim, "clicked", True), after_click())
        got = sl.join_draft("D", _session_cls=self._session(self.Page(seats)),
                            _slot_fn=lambda d, n: None,
                            _sleep_fn=lambda _s: None)
        assert claim.clicked and not wrapper.clicked
        assert got == 2

    def test_already_seated_is_detected_from_the_DOM_not_the_api(
            self, monkeypatch):
        """draft_order lags by over a minute; a re-run inside that window
        claimed a SECOND seat."""
        monkeypatch.setattr(sl, "LIVE_WRITES_OK", lambda: True)
        monkeypatch.setattr(sl, "authenticate", lambda p: True)
        claim = self.Btn()
        seats = [self.Seat("awarmwalrus"), self.Seat("awarmwalrus"),
                 self.Seat("CLAIM Team 2", claim)]
        got = sl.join_draft("D", _session_cls=self._session(self.Page(seats)),
                            _slot_fn=lambda d, n: None,   # API still blind
                            _sleep_fn=lambda _s: None)
        assert got == 1
        assert not claim.clicked, "must not claim a second seat"

    def test_the_api_shortcut_still_works_when_it_has_caught_up(self,
                                                                monkeypatch):
        monkeypatch.setattr(sl, "LIVE_WRITES_OK", lambda: True)
        got = sl.join_draft("D", _slot_fn=lambda d, n: 5,
                            _sleep_fn=lambda _s: None)
        assert got == 5

    def test_it_can_target_a_named_seat(self, monkeypatch):
        monkeypatch.setattr(sl, "LIVE_WRITES_OK", lambda: True)
        monkeypatch.setattr(sl, "authenticate", lambda p: True)
        three, four = self.Btn(), self.Btn()
        seats = [self.Seat("CLAIM Team 3", three),
                 self.Seat("CLAIM Team 3", three),
                 self.Seat("CLAIM Team 4", four),
                 self.Seat("CLAIM Team 4", four)]

        def took():
            seats[2].label = seats[3].label = "awarmwalrus"
        four.click = lambda: (setattr(four, "clicked", True), took())
        sl.join_draft("D", slot=4, _session_cls=self._session(self.Page(seats)),
                      _slot_fn=lambda d, n: None, _sleep_fn=lambda _s: None)
        assert four.clicked and not three.clicked

    def test_a_full_draft_refuses_rather_than_clicking_something_else(
            self, monkeypatch):
        monkeypatch.setattr(sl, "LIVE_WRITES_OK", lambda: True)
        monkeypatch.setattr(sl, "authenticate", lambda p: True)
        page = self.Page([self.Seat("johannhof"), self.Seat("aykutb")])
        try:
            sl.join_draft("D", _session_cls=self._session(page),
                          _slot_fn=lambda d, n: None, _sleep_fn=lambda _s: None)
            assert False, "should have refused"
        except RuntimeError as e:
            assert "no free seat" in str(e)

    def test_a_click_that_does_not_take_is_reported_as_failure(self,
                                                              monkeypatch):
        monkeypatch.setattr(sl, "LIVE_WRITES_OK", lambda: True)
        monkeypatch.setattr(sl, "authenticate", lambda p: True)
        page = self.Page([self.Seat("CLAIM Team 2", self.Btn())])
        try:
            sl.join_draft("D", _session_cls=self._session(page),
                          _slot_fn=lambda d, n: None, _sleep_fn=lambda _s: None)
            assert False, "should have refused"
        except RuntimeError as e:
            assert "did not take" in str(e)

    def test_writes_off_means_no_seat_is_claimed(self, monkeypatch):
        monkeypatch.setattr(sl, "LIVE_WRITES_OK", lambda: False)
        try:
            sl.join_draft("D", _slot_fn=lambda d, n: None)
            assert False, "should have refused"
        except RuntimeError as e:
            assert "writes are off" in str(e)


class TestCloseCallNotes:
    """Enrichment fires only where the engine cannot separate the candidates,
    and only when switched on -- it MISSED the bar set before the experiment
    (agreement with the engine rose on both test drafts), so it ships off."""

    IDX = {"1": {"name": "Alpha", "positions": ["RB"], "team": "KC",
                 "injury_status": "PUP", "injury_body_part": "Knee",
                 "age": 24, "years_exp": 2, "depth_chart_order": 2},
           "2": {"name": "Beta", "positions": ["RB"], "team": "KC",
                 "age": 30, "years_exp": 9, "depth_chart_order": 1}}

    def _board(self, gap):
        return [{"player_key": "1", "adj_value": 5.00, "positions": ["RB"]},
                {"player_key": "2", "adj_value": 5.00 - gap,
                 "positions": ["RB"]}]

    def test_it_does_nothing_when_switched_off(self, monkeypatch):
        monkeypatch.setattr(sl, "CLOSE_CALL_NOTES", False)
        board = self._board(0.1)
        assert sl._annotate_close_calls(board, self.IDX, {}) == 0
        assert all("notes" not in c for c in board)

    def test_close_candidates_get_notes(self, monkeypatch):
        monkeypatch.setattr(sl, "CLOSE_CALL_NOTES", True)
        board = self._board(0.1)
        assert sl._annotate_close_calls(board, self.IDX, {}) == 2
        assert "Knee" in board[0]["notes"]["injury"]

    def test_a_clear_leader_gets_nothing(self, monkeypatch):
        """No tie, no tiebreak needed -- and the frozen payload stays frozen."""
        monkeypatch.setattr(sl, "CLOSE_CALL_NOTES", True)
        board = self._board(5.0)
        assert sl._annotate_close_calls(board, self.IDX, {}) == 0
        assert all("notes" not in c for c in board)

    def test_the_depth_chart_note_names_the_man_ahead(self, monkeypatch):
        monkeypatch.setattr(sl, "CLOSE_CALL_NOTES", True)
        board = self._board(0.1)
        sl._annotate_close_calls(board, self.IDX, {})
        assert "behind Beta" in board[0]["notes"]["role"]

    def test_a_one_player_board_is_not_a_close_call(self, monkeypatch):
        monkeypatch.setattr(sl, "CLOSE_CALL_NOTES", True)
        assert sl._annotate_close_calls(
            [{"player_key": "1", "adj_value": 1.0}], self.IDX, {}) == 0


class TestEntryNotesArePayloadSafe:
    """A row the engine CAN separate keeps the exact frozen payload."""

    def test_an_unannotated_row_is_byte_identical(self):
        c = {"player_key": "1", "name": "A", "positions": ["RB"], "vor": 1.0}
        assert agent._entry_with_notes(c) == agent._entry(c)

    def test_an_annotated_row_carries_its_notes(self):
        c = {"player_key": "1", "name": "A", "positions": ["RB"], "vor": 1.0,
             "notes": {"role": "RB2 behind X"}}
        assert agent._entry_with_notes(c)["notes"] == {"role": "RB2 behind X"}


class TestAccountIsASetting:
    """The owner has more than one Sleeper account -- a personal one holding
    the real league team, and a bot account for mocks. The username has to
    track the TOKEN, or writes land as the wrong person and every seat lookup
    (which keys off the display name) silently finds nothing."""

    def test_it_defaults_to_the_owner(self):
        assert sl.USERNAME

    def test_the_environment_overrides_it(self, monkeypatch):
        monkeypatch.setenv("WES_SLEEPER_USER", "GMBartimusPrime")
        import importlib
        importlib.reload(sl)
        try:
            assert sl.USERNAME == "GMBartimusPrime"
        finally:
            monkeypatch.delenv("WES_SLEEPER_USER")
            importlib.reload(sl)

    def test_join_draft_uses_the_configured_account(self, monkeypatch):
        monkeypatch.setattr(sl, "USERNAME", "GMBartimusPrime")
        monkeypatch.setattr(sl, "LIVE_WRITES_OK", lambda: True)
        seen = []
        sl.join_draft("D", _slot_fn=lambda d, n: seen.append(n) or 4)
        assert seen == ["GMBartimusPrime"]

    def test_banter_knows_which_name_is_its_own(self, monkeypatch):
        """`me` is how it avoids answering itself; a stale default would make
        two agents in a room an infinite loop with an audience."""
        monkeypatch.setattr(sl, "USERNAME", "GMBartimusPrime")
        import wes_banter
        assert wes_banter.Banter("D").me == "GMBartimusPrime"

    def test_an_explicit_name_still_wins(self):
        import wes_banter
        assert wes_banter.Banter("D", me="someone-else").me == "someone-else"


class TestPerAccountToken:
    """A second account must be ADDITIVE. The shared WES_SLEEPER_TOKEN holds
    the personal account that owns the real league team, so a bot account that
    replaced it would silently stop WES drafting on the day."""

    def test_the_per_account_variable_wins(self, monkeypatch):
        monkeypatch.setenv("WES_SLEEPER_TOKEN", "shared")
        monkeypatch.setenv("WES_SLEEPER_TOKEN_GMBARTIMUSPRIME", "bot")
        assert sl._read_token("GMBartimusPrime") == "bot"

    def test_the_shared_one_is_the_fallback(self, monkeypatch):
        monkeypatch.setenv("WES_SLEEPER_TOKEN", "shared")
        monkeypatch.delenv("WES_SLEEPER_TOKEN_AWARMWALRUS", raising=False)
        assert sl._read_token("awarmwalrus") == "shared"

    def test_adding_a_bot_account_does_not_displace_the_owner(self,
                                                              monkeypatch):
        """The whole point: both work at once."""
        monkeypatch.setenv("WES_SLEEPER_TOKEN", "owner-token")
        monkeypatch.setenv("WES_SLEEPER_TOKEN_GMBARTIMUSPRIME", "bot-token")
        assert sl._read_token("awarmwalrus") == "owner-token"
        assert sl._read_token("GMBartimusPrime") == "bot-token"

    def test_punctuation_in_a_name_does_not_break_the_lookup(self,
                                                             monkeypatch):
        monkeypatch.setenv("WES_SLEEPER_TOKEN_ABC123", "x")
        assert sl._read_token("a-b.c_123") == "x"

    def test_no_username_still_reads_the_shared_one(self, monkeypatch):
        monkeypatch.setenv("WES_SLEEPER_TOKEN", "shared")
        assert sl._read_token() == "shared"

    def test_nothing_configured_is_empty_not_an_exception(self, monkeypatch):
        monkeypatch.delenv("WES_SLEEPER_TOKEN", raising=False)
        monkeypatch.delenv("WES_SLEEPER_TOKEN_NOBODY", raising=False)
        monkeypatch.setattr(sl.os, "name", "posix")
        assert sl._read_token("nobody") == ""


class TestPickVerificationIsOursOnly:
    """A check that ANOTHER MANAGER can satisfy verifies nothing.

    Observed live 2026-08-21: the loop logged "DRAFTED Trey McBride" while
    McBride went to slot 3 at pick 23 and our own pick 21 was Lamar Jackson.
    Our click had missed; the owner took the same player two picks later; the
    verification saw the id in the pick list and called it ours."""

    def _submit(self, monkeypatch, picks, slot):
        monkeypatch.setattr(sl, "LIVE_WRITES_OK", lambda: True)
        monkeypatch.setattr(sl, "authenticate", lambda p: True)
        monkeypatch.setattr(sl, "_click_pick", lambda *a, **k: None)

        class S:
            def __enter__(_s):
                return TestWindowedList.Page(TestWindowedList.Box(),
                                             lambda: [])

            def __exit__(_s, *a):
                return False
        return sl.submit_pick("D", "77", "Target Guy", _session_cls=S,
                              _picks_fn=lambda d: picks, _sleep_fn=lambda x: None,
                              slot=slot)

    def test_our_own_pick_verifies(self, monkeypatch):
        got = self._submit(monkeypatch,
                           [{"player_id": "77", "draft_slot": 4,
                             "pick_no": 20}], slot=4)
        assert got is True

    def test_ANOTHER_managers_pick_is_refused(self, monkeypatch):
        """The live bug, pinned."""
        try:
            self._submit(monkeypatch,
                         [{"player_id": "77", "draft_slot": 3, "pick_no": 23}],
                         slot=1)
            assert False, "must not report someone else's pick as ours"
        except RuntimeError as e:
            assert "not by us" in str(e) and "slot 3" in str(e)

    def test_it_still_waits_when_the_pick_has_not_landed_yet(self,
                                                             monkeypatch):
        try:
            self._submit(monkeypatch, [], slot=1)
            assert False, "should have raised"
        except RuntimeError as e:
            assert "never appeared" in str(e)

    def test_without_a_slot_it_falls_back_to_the_old_looser_check(self,
                                                                 monkeypatch):
        """Callers that cannot know their slot are not broken by this, but the
        draft loop always passes one."""
        got = self._submit(monkeypatch,
                           [{"player_id": "77", "draft_slot": 9,
                             "pick_no": 5}], slot=None)
        assert got is True


class TestAutopickToggle:
    """Sleeper turns AUTO-PICK ON BY ITSELF after a missed pick and leaves it
    on. So one missed pick does not cost one pick -- it costs the rest of the
    draft, silently, while every click lands on nothing. That is exactly what
    happened on 2026-08-21 (owner's diagnosis)."""

    class Slider:
        def __init__(self, state):
            self.state = state
            self.clicks = 0

        def click(self):
            self.clicks += 1
            self.state["on"] = not self.state["on"]

    class Page:
        def __init__(self, on, slider=None):
            self.state = {"on": on}
            self.slider = slider or TestAutopickToggle.Slider(self.state)

        def evaluate(self, _js, _sel=None):
            return self.state["on"]

        def query_selector(self, _sel):
            return self.slider

        def wait_for_timeout(self, _ms):
            pass

    def test_it_reads_the_state(self):
        assert sl.autopick_on(self.Page(True)) is True
        assert sl.autopick_on(self.Page(False)) is False

    def test_turning_it_off_when_it_is_on(self):
        page = self.Page(True)
        assert sl.set_autopick(page, False) is False
        assert page.slider.clicks == 1

    def test_it_does_not_click_when_already_off(self):
        """Toggling a control that is already right would turn it ON."""
        page = self.Page(False)
        assert sl.set_autopick(page, False) is False
        assert page.slider.clicks == 0

    def test_it_verifies_rather_than_trusting_the_click(self):
        """A control whose purpose is to act instead of us is the worst place
        to be optimistic."""
        page = self.Page(True)
        page.slider = self.Slider({"on": True})   # click changes nothing real
        got = sl.set_autopick(page, False, _tries=3)
        assert got is True, "must report the state it actually ended in"
        assert page.slider.clicks == 3, "must have kept trying"

    def test_a_missing_control_is_None_not_a_crash(self):
        class NoControl(TestAutopickToggle.Page):
            def evaluate(self, _js, _sel=None):
                return None
        assert sl.autopick_on(NoControl(False)) is None
        assert sl.set_autopick(NoControl(False), False) is None


class TestDisabledButtonIsNotAVeto:
    """The `disable` class is not a reliable signal of whose turn it is: with
    37 picks made, pick 38 ours and autopick confirmed OFF, it still read
    disabled after twenty seconds (2026-08-22). Refusing there forfeited a
    pick we were entitled to make -- and it is now safe to try, because
    verification requires the pick's slot to be ours."""

    def test_it_clicks_a_button_that_still_reads_disabled(self, monkeypatch):
        monkeypatch.setattr(sl, "LIVE_WRITES_OK", lambda: True)
        monkeypatch.setattr(sl, "authenticate", lambda p: True)
        monkeypatch.setattr(sl, "ENABLE_WAIT_TRIES", 2)
        btn = TestSubmitPick.FakeBtn("draft-button disable")
        row = TestSubmitPick.FakeRow("Target Guy", btn)

        class Page(TestWindowedList.Page):
            def wait_for_selector(self, *a, **k):
                return True

        page = Page(TestWindowedList.Box(), lambda: [row])

        class S:
            def __enter__(_s):
                return page

            def __exit__(_s, *a):
                return False
        ok = sl.submit_pick("D", "42", "Target Guy", _session_cls=S,
                            _picks_fn=lambda d: [{"player_id": "42",
                                                  "draft_slot": 2}],
                            _sleep_fn=lambda _s: None, slot=2)
        assert ok is True
        assert btn.clicked, "must attempt rather than stand down"



class _Grid:
    """The ReactVirtualized container the scroller aims at."""

    def bounding_box(self):
        return {"x": 0, "y": 0, "width": 900, "height": 370}

    def inner_text(self):
        return "row"


class TestMustFillClosesTheBoard:
    """The engine has to stop OFFERING the running back.

    A 15-round mock finished WR6/RB7/TE1/QB1 with no kicker and no defense, the
    model correctly noting each time that a skill player had more value -- so
    the fix belongs in the choice set, not the prompt (2026-08-22).
    """

    ROUNDS = 4

    @staticmethod
    def _get(path):
        if "/league/" in path:
            return {"scoring_settings": {"rec": 1},
                    "roster_positions": ["RB", "K", "BN"]}
        if "/picks" in path:
            return []
        if "/draft/" in path:
            return {"status": "drafting",
                    "settings": {"teams": 2,
                                 "rounds": TestMustFillClosesTheBoard.ROUNDS},
                    "slot_to_roster_id": {"1": 1, "2": 2},
                    "type": "snake"}
        return {}

    def _board(self, mine_positions):
        """`mine_positions` is what we already hold, one pick per entry."""
        idx = {"1": {"name": "Backfield", "positions": ["RB"], "team": "KC",
                     "roster_status": "Active"},
               "2": {"name": "Bootleg", "positions": ["K"], "team": "SF",
                     "roster_status": "Active"}}
        for i, pos in enumerate(mine_positions):
            idx[f"h{i}"] = {"name": f"Held{i}", "positions": [pos],
                            "team": "NYJ", "roster_status": "Active"}
        pool = [{"name": v["name"], "positions": v["positions"],
                 "team": v["team"], "gp": 17.0,
                 # The kicker is deliberately the WORSE player, which is the
                 # whole difficulty: on value it should never be chosen.
                 "cats": {"Rec": 5.0 if v["positions"] == ["K"] else 90.0}}
                for v in idx.values()]
        picks = [{"pick_no": i + 1, "draft_slot": 1, "player_id": f"h{i}",
                  "metadata": {"position": pos}}
                 for i, pos in enumerate(mine_positions)]
        out = sl.draft_candidates(
            "L", "D", 1, limit=8, _index_fn=lambda: idx, _pool_fn=lambda: pool,
            _picks=picks, _get_fn=lambda p, **k: self._get(p))
        assert not isinstance(out, str), out
        return out

    def test_with_picks_to_spare_the_kicker_does_not_crowd_the_board(self):
        out = self._board([])
        assert out["must_fill"] == []
        assert "Backfield" in {c["name"] for c in out["candidates"]}

    def test_on_the_last_pick_only_the_empty_slot_is_offered(self):
        """Three of four picks spent, RB filled, K still empty."""
        out = self._board(["RB", "RB", "RB"])
        assert out["must_fill"] == ["K"]
        assert {c["name"] for c in out["candidates"]} == {"Bootleg"}

    def test_the_constraint_is_named_for_the_model(self):
        out = self._board(["RB", "RB", "RB"])
        assert "K" in out["must_fill"]

    def test_it_never_returns_an_empty_board(self):
        """A draft pick is MANDATORY. If the pool cannot fill the slot, an
        incomplete roster still beats forfeiting the pick."""
        idx = {"1": {"name": "Backfield", "positions": ["RB"], "team": "KC",
                     "roster_status": "Active"}}
        pool = [{"name": "Backfield", "positions": ["RB"], "team": "KC",
                 "gp": 17.0, "cats": {"Rec": 90.0}}]
        picks = [{"pick_no": i + 1, "draft_slot": 1, "player_id": f"h{i}",
                  "metadata": {"position": "RB"}} for i in range(3)]
        out = sl.draft_candidates(
            "L", "D", 1, limit=8, _index_fn=lambda: idx, _pool_fn=lambda: pool,
            _picks=picks, _get_fn=lambda p, **k: self._get(p))
        assert not isinstance(out, str), out
        assert out["candidates"], "must still offer somebody"
