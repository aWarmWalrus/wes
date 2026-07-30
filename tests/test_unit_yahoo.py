"""Unit tests for wes_yahoo (ticket #029 P0) — network-free, browser-free.

The official Yahoo API was abandoned 2026-07-17 (design §1/§10); wes_yahoo is now
a Playwright browser adapter. These tests cover the two things that ARE stable
without a live UI: the normalized-dict FORMATTERS (reused verbatim from the API
era) and graceful DEGRADATION (no session / no Playwright / selector-not-written
must return a string, never raise). The DOM extractors are stubs whose selectors
need the real logged-in page, so they're exercised by the WES_YAHOO_LIVE=1 canary
once a session exists, not here.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "pc"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import wes_yahoo as wy  # noqa: E402


# The normalized contract (module docstring): the formatters + everything
# downstream depend on exactly these dict shapes, whatever the source.
ROSTER = [
    {"name": "Nikola Jokic", "team": "DEN", "positions": ["C"],
     "slot": "C", "status": "", "player_key": "466.p.6014"},
    {"name": "Cam Thomas", "team": "BKN", "positions": ["PG", "SG"],
     "slot": "BN", "status": "INJ", "player_key": "466.p.5482"},
]
SCORING = {"scoring_type": "head", "categories": ["PTS", "REB", "AST"]}


class TestFormatRoster:
    def test_compact_one_line_per_player(self):
        out = wy.format_roster(ROSTER, "Dinosaurs")
        assert "Dinosaurs" in out
        # one header + one line per player: the ~400-token budget (design §8.3)
        assert len(out.splitlines()) == 3
        assert "Nikola Jokic" in out and "DEN" in out

    def test_multi_position_eligibility_rendered(self):
        assert "PG/SG" in wy.format_roster(ROSTER)

    def test_flags_injuries(self):
        # status drives lineup decisions -- it must survive to the model
        assert "INJ" in wy.format_roster(ROSTER)

    def test_handles_empty(self):
        assert "empty" in wy.format_roster([]).lower()

    def test_tolerates_missing_optional_keys(self):
        # a scraper that only got a name must not crash the formatter
        assert "Wemby" in wy.format_roster([{"name": "Wemby"}])


class TestFormatScoring:
    def test_names_the_format(self):
        out = wy.format_scoring(SCORING)
        assert "head-to-head" in out
        assert "PTS" in out and "REB" in out

    def test_unknown_categories_do_not_crash(self):
        assert "unknown" in wy.format_scoring({"scoring_type": "roto"})


class TestDegradation:
    """A Yahoo problem — no session, no Playwright, unwritten scraper — must
    never raise into a turn; every entry point returns a string."""

    def test_no_playwright_degrades_to_hint(self, monkeypatch):
        monkeypatch.setattr(wy, "_have_playwright", lambda: False)
        for out in (wy.my_teams(), wy.roster("466.l.1.t.1"),
                    wy.league_scoring("466.l.1")):
            assert isinstance(out, str)
            assert "isn't installed" in out

    def test_no_session_degrades_to_hint(self, monkeypatch):
        monkeypatch.setattr(wy, "_have_playwright", lambda: True)
        monkeypatch.setattr(wy, "has_session", lambda: False)
        for out in (wy.my_teams(), wy.roster("466.l.1.t.1"),
                    wy.league_scoring("466.l.1")):
            assert isinstance(out, str)
            assert "one-time browser sign-in" in out

    def test_scraper_on_barren_page_returns_empty_not_raises(self, monkeypatch):
        # Session present but the page has no roster table (wrong page / reskin):
        # the extractor finds nothing and roster() reports 'empty', never raises.
        monkeypatch.setattr(wy, "_have_playwright", lambda: True)
        monkeypatch.setattr(wy, "has_session", lambda: True)

        class _FakePage:
            def goto(self, *a, **k):
                pass

            def query_selector_all(self, *a, **k):
                return []  # no player rows

        class _FakeSession:
            def __init__(self, *a, **k):
                pass

            def __enter__(self):
                return _FakePage()

            def __exit__(self, *exc):
                return False

        monkeypatch.setattr(wy, "_Session", _FakeSession)
        out = wy.roster("466.l.1.t.1")
        assert isinstance(out, str)
        assert "empty" in out.lower()

    def test_scrape_swallows_unexpected_errors(self, monkeypatch):
        monkeypatch.setattr(wy, "_have_playwright", lambda: True)
        monkeypatch.setattr(wy, "has_session", lambda: True)

        class _BoomSession:
            def __init__(self, *a, **k):
                pass

            def __enter__(self):
                raise OSError("browser crashed")

            def __exit__(self, *exc):
                return False

        monkeypatch.setattr(wy, "_Session", _BoomSession)
        out = wy.league_scoring("466.l.1")
        assert isinstance(out, str)
        assert "couldn't reach" in out


class TestLoggedIn:
    """logged_in() is the real auth check; it must degrade to False, not raise."""

    def test_false_when_not_configured(self, monkeypatch):
        monkeypatch.setattr(wy, "_have_playwright", lambda: False)
        assert wy.logged_in() is False

    def test_false_when_launch_errors(self, monkeypatch):
        monkeypatch.setattr(wy, "configured", lambda: True)

        class _BoomSession:
            def __init__(self, *a, **k):
                pass

            def __enter__(self):
                raise OSError("no browser")

            def __exit__(self, *exc):
                return False

        monkeypatch.setattr(wy, "_Session", _BoomSession)
        assert wy.logged_in() is False

    def _session_with_cookies(self, cookies):
        class _Ctx:
            def cookies(self):
                return cookies

        class _Page:
            context = _Ctx()

        class _Sess:
            def __init__(self, *a, **k):
                pass

            def __enter__(self):
                return _Page()

            def __exit__(self, *exc):
                return False

        return _Sess

    def test_true_when_auth_cookies_present(self, monkeypatch):
        monkeypatch.setattr(wy, "configured", lambda: True)
        cookies = [{"name": "T", "domain": ".yahoo.com"},
                   {"name": "Y", "domain": ".yahoo.com"}]
        monkeypatch.setattr(wy, "_Session", self._session_with_cookies(cookies))
        assert wy.logged_in() is True

    def test_false_when_only_junk_cookies(self, monkeypatch):
        monkeypatch.setattr(wy, "configured", lambda: True)
        # a profile with tracking cookies but no Yahoo auth => signed out
        cookies = [{"name": "_ga", "domain": ".yahoo.com"},
                   {"name": "A1", "domain": ".google.com"}]  # wrong domain
        monkeypatch.setattr(wy, "_Session", self._session_with_cookies(cookies))
        assert wy.logged_in() is False


class TestTeamRegistry:
    """teams.yaml resolution — identities + policy, no secrets. Never raises."""

    CONFIG = {"teams": [
        {"name": "Dinosaurs", "league_key": "466.l.12345",
         "team_key": "466.l.12345.t.3"},
        {"name": "Work League", "league_key": "466.l.67890",
         "team_key": "466.l.67890.t.7"},
    ]}

    def test_missing_file_degrades_to_empty(self, monkeypatch, tmp_path):
        monkeypatch.setattr(wy, "_teams_cache", None)
        monkeypatch.setattr(wy, "TEAMS_FILE", str(tmp_path / "nope.yaml"))
        assert wy.configured_teams() == []

    def test_default_is_first_team(self, monkeypatch):
        monkeypatch.setattr(wy, "configured_teams", lambda: self.CONFIG["teams"])
        team, err = wy._resolve_team()
        assert err is None and team["name"] == "Dinosaurs"

    def test_named_substring_match(self, monkeypatch):
        monkeypatch.setattr(wy, "configured_teams", lambda: self.CONFIG["teams"])
        team, err = wy._resolve_team("work")
        assert err is None and team["team_key"] == "466.l.67890.t.7"

    def test_unknown_name_lists_options(self, monkeypatch):
        monkeypatch.setattr(wy, "configured_teams", lambda: self.CONFIG["teams"])
        team, err = wy._resolve_team("Sharks")
        assert team is None
        assert "Dinosaurs" in err and "Work League" in err

    def test_no_config_returns_none_none(self, monkeypatch):
        monkeypatch.setattr(wy, "configured_teams", lambda: [])
        assert wy._resolve_team("anything") == (None, None)


class TestFantasyMyTeam:
    TEAM = {"name": "Dinosaurs", "league_key": "466.l.12345",
            "team_key": "466.l.12345.t.3"}

    def test_combines_roster_and_scoring(self, monkeypatch):
        monkeypatch.setattr(wy, "_resolve_team", lambda team=None: (self.TEAM, None))
        monkeypatch.setattr(wy, "roster", lambda k: "Roster (2):\n  Jokic\n  Thomas")
        monkeypatch.setattr(wy, "league_scoring",
                            lambda k: "Scoring: head-to-head. Categories that count: PTS.")
        out = wy.fantasy_my_team()
        assert out.startswith("Dinosaurs")
        assert "Roster (2)" in out and "Categories that count" in out

    def test_roster_degradation_surfaced_without_scoring(self, monkeypatch):
        monkeypatch.setattr(wy, "_resolve_team", lambda team=None: (self.TEAM, None))
        monkeypatch.setattr(wy, "roster", lambda k: wy._UNAVAILABLE)
        called = []
        monkeypatch.setattr(wy, "league_scoring",
                            lambda k: called.append(k) or "Scoring: x")
        out = wy.fantasy_my_team()
        assert out == wy._UNAVAILABLE
        assert called == []  # a broken read isn't dressed up with scoring

    def test_unknown_team_name_returns_error(self, monkeypatch):
        monkeypatch.setattr(wy, "_resolve_team",
                            lambda team=None: (None, "no team called 'x'"))
        assert wy.fantasy_my_team("x") == "no team called 'x'"

    def test_no_config_falls_back_to_listing(self, monkeypatch):
        monkeypatch.setattr(wy, "_resolve_team", lambda team=None: (None, None))
        monkeypatch.setattr(wy, "my_teams",
                            lambda: "Your NBA fantasy teams:\n  Dinosaurs (nba.l.1.t.3)")
        out = wy.fantasy_my_team()
        assert "Dinosaurs" in out and "teams.yaml" in out

    def test_no_config_passes_through_degradation_hint(self, monkeypatch):
        # no teams.yaml AND no session: surface the sign-in hint, not the setup one
        monkeypatch.setattr(wy, "_resolve_team", lambda team=None: (None, None))
        monkeypatch.setattr(wy, "my_teams", lambda: wy._NOT_CONFIGURED)
        assert wy.fantasy_my_team() == wy._NOT_CONFIGURED


class TestMultiSportUrls:
    """#029 P7: sport comes from the dotted key, so no caller signature changed.

    The asymmetry these pin: NFL is football.../f1/, NOT football.../nfl/. That
    is the single most likely thing for someone to 'fix' by analogy and break.
    """

    def test_sport_derived_from_key_prefix(self):
        assert wy._sport_of("nfl.l.957011.t.4") == "nfl"
        assert wy._sport_of("nba.l.114020.t.1") == "nba"

    def test_legacy_numeric_and_junk_keys_default_to_nba(self):
        # Historic NBA keys were "466.l.12345.t.3"; teams.yaml still allows them.
        assert wy._sport_of("466.l.12345.t.3") == "nba"
        assert wy._sport_of("") == "nba"
        assert wy._sport_of(None) == "nba"
        assert wy._sport_of("quidditch.l.1.t.1") == "nba"

    def test_nfl_team_url_uses_f1_not_nfl(self):
        url = wy._team_url("nfl.l.957011.t.4")
        assert url == ("https://football.fantasysports.yahoo.com"
                       "/f1/957011/4")
        assert "/nfl/" not in url

    def test_nba_team_url_unchanged(self):
        assert wy._team_url("nba.l.114020.t.1") == (
            "https://basketball.fantasysports.yahoo.com/nba/114020/1")

    def test_league_url_and_suffix(self):
        assert wy._league_url("nfl.l.424494", "settings") == (
            "https://football.fantasysports.yahoo.com/f1/424494/settings")
        assert wy._league_url("nba.l.114020") == (
            "https://basketball.fantasysports.yahoo.com/nba/114020")

    def test_unknown_sport_degrades_to_nba_site(self):
        assert wy._site("kabaddi")["path"] == "nba"
        assert wy._site(None)["home"] == wy.FANTASY_HOME

    def test_sports_enumerates_both(self):
        assert set(wy.sports()) == {"nba", "nfl"}

    def test_roster_navigates_to_the_football_host(self, monkeypatch):
        """roster() with an NFL key must hit the football site — the whole point
        of the parameterization, and invisible without checking the URL."""
        monkeypatch.setattr(wy, "_have_playwright", lambda: True)
        monkeypatch.setattr(wy, "has_session", lambda: True)
        seen = []

        class _FakePage:
            def goto(self, url, *a, **k):
                seen.append(url)

            def query_selector_all(self, *a, **k):
                return []

        class _FakeSession:
            def __init__(self, *a, **k):
                pass

            def __enter__(self):
                return _FakePage()

            def __exit__(self, *exc):
                return False

        monkeypatch.setattr(wy, "_Session", _FakeSession)
        wy.roster("nfl.l.957011.t.4")
        assert seen == ["https://football.fantasysports.yahoo.com/f1/957011/4"]


class TestRosterCellParsing:
    """The 'team - positions' cell and the injury status, both of which parsed
    WRONG on football until 2026-07-29 and had no coverage at all. Failure here
    is silent and total: with no positions, no player is eligible for any slot
    and the optimizer benches the entire roster."""

    class _El:
        def __init__(self, text):
            self._t = text

        def inner_text(self):
            return self._t

    def _row(self, cells):
        outer = self

        class _Row:
            def query_selector(self, sel):
                return outer._El(cells[sel]) if sel in cells else None
        return _Row()

    def test_nba_shape_from_ysf_player_detail(self):
        row = self._row({".ysf-player-detail": "Bkn - PG,SG"})
        assert wy._detail_team_positions(row) == ("Bkn", ["PG", "SG"])

    def test_nfl_shape_from_fz_xxs_when_detail_holds_the_game(self):
        """The exact live football layout: the NBA selector holds the GAME time,
        so the parser must fall through to the one that has 'Phi - QB'."""
        row = self._row({".ysf-player-detail": "Sun 1:25 pm vs Was",
                         "span.Fz-xxs": "Phi - QB"})
        assert wy._detail_team_positions(row) == ("Phi", ["QB"])

    def test_game_string_is_captured_separately(self):
        row = self._row({".ysf-player-detail": "Sun 1:25 pm vs Was",
                         "span.Fz-xxs": "Phi - QB"})
        assert wy._detail_game(row) == "Sun 1:25 pm vs Was"

    def test_game_is_empty_when_the_cell_holds_team_positions(self):
        row = self._row({".ysf-player-detail": "Bkn - PG,SG"})
        assert wy._detail_game(row) == ""

    def test_a_game_string_containing_a_dash_is_not_read_as_positions(self):
        # The guard against the fallback matching junk.
        row = self._row({".ysf-player-detail": "Sun 1:25 pm - vs Washington"})
        assert wy._detail_team_positions(row) == ("", [])

    def test_missing_cells_degrade_to_blank(self):
        assert wy._detail_team_positions(self._row({})) == ("", [])
        assert wy._detail_game(self._row({})) == ""

    def test_offseason_blank_detail(self):
        row = self._row({".ysf-player-detail": ""})
        assert wy._detail_team_positions(row) == ("", [])

    @pytest.mark.parametrize("junk", [
        "Video ForecastNo new player Notes",
        "No new player Notes",
        "Video ForecastNew Player Note",
    ])
    def test_player_note_chrome_is_not_an_injury_status(self, junk):
        """Football puts note chrome in the status span; left alone it renders a
        fake injury flag on every healthy player."""
        assert wy._clean_status(junk) == ""

    @pytest.mark.parametrize("status", ["O", "Q", "IR", "GTD", "SUSP", "D"])
    def test_real_statuses_survive(self, status):
        assert wy._clean_status(status) == status

    def test_unknown_short_status_is_kept_verbatim(self):
        """Deliberately NOT a whitelist: an unrecognized status is harmless (it
        just isn't in _OUT_STATUS, so the player stays startable), whereas
        dropping a real one would silently start an injured player."""
        assert wy._clean_status("PPD") == "PPD"

    def test_whitespace_collapsed(self):
        assert wy._clean_status("  O  ") == "O"


class TestMyTeamOwnership:
    """The opponent trap: a league page links the current-week OPPONENT too, so
    ownership must come from the 'My Team' nav href, never from link text.
    Observed live 2026-07-29 on nfl.l.957011 (t.4 = owner, t.8 = opponent)."""

    class _Anchor:
        def __init__(self, text, href):
            self._t, self._h = text, href

        def inner_text(self):
            return self._t

        def get_attribute(self, name):
            return {"href": self._h, "title": None}.get(name)

    def _page(self, anchors):
        outer = self

        class _P:
            def query_selector_all(self, sel):
                return [outer._Anchor(t, h) for t, h in anchors]
        return _P()

    def test_picks_the_my_team_nav_link_not_the_opponent(self):
        page = self._page([
            ("Brickhouse jp", "/f1/957011/8"),      # the opponent, listed first
            ("My Team", "/f1/957011/4"),            # the truth
            ("Players", "/f1/957011/players"),
        ])
        assert wy._my_team_key(page, "nfl") == "nfl.l.957011.t.4"

    def test_ignores_text_markers_on_other_teams_pages(self):
        # "Edit Team" appears in nav on every team page — it must not count.
        page = self._page([("Edit Team", "/f1/957011/8")])
        assert wy._my_team_key(page, "nfl") == ""

    def test_absolute_hrefs_work_too(self):
        page = self._page([
            ("My Team",
             "https://football.fantasysports.yahoo.com/f1/424494/5?src=nav")])
        assert wy._my_team_key(page, "nfl") == "nfl.l.424494.t.5"

    def test_no_nav_link_returns_empty_not_a_guess(self):
        assert wy._my_team_key(self._page([]), "nfl") == ""

    def test_wrong_sport_path_is_rejected(self):
        # An /nba/ href must not be read as an NFL team.
        page = self._page([("My Team", "/nba/114020/1")])
        assert wy._my_team_key(page, "nfl") == ""

    def test_extract_league_ids_dedupes(self):
        page = self._page([
            ("a", "/f1/957011/4"), ("b", "/f1/957011/8"),
            ("c", "/f1/424494/5"), ("d", "/f1/424494"),
        ])
        assert wy._extract_league_ids(page, "nfl") == ["957011", "424494"]

    def test_extract_my_teams_builds_sport_prefixed_keys(self):
        page = self._page([("Charles's Pop", "/f1/957011/4")])
        assert wy._extract_my_teams(page, "nfl") == [
            ("Charles's Pop", "nfl.l.957011.t.4")]


class TestKeyHelpers:
    def test_league_and_team_extracted_from_yahoo_key(self):
        assert wy._league_of("466.l.12345.t.3") == "12345"
        assert wy._team_of("466.l.12345.t.3") == "3"

    def test_key_helpers_tolerate_junk(self):
        # a malformed key must not raise mid-URL-build
        assert isinstance(wy._league_of("nonsense"), str)
        assert wy._team_of("nonsense") == ""


@pytest.mark.skipif(os.environ.get("WES_YAHOO_LIVE") != "1",
                    reason="live Yahoo canary; set WES_YAHOO_LIVE=1 (needs a "
                           "signed-in profile)")
class TestLiveCanary:
    """Selector-drift canary — the DOM scrapers are written against Yahoo's live
    markup, which they reskin without notice. This proves a real read still
    parses and catches drift. Point it at a real team with
    WES_YAHOO_TEST_TEAM=nba.l.<league>.t.<team> (kept out of the repo)."""

    def _no_error(self, out):
        for bad in ("isn't connected", "sign-in", "couldn't reach",
                    "isn't wired up yet", "isn't installed"):
            assert bad not in out, f"degraded: {out!r}"

    def test_my_teams_returns_real_data(self):
        out = wy.my_teams()
        self._no_error(out)
        assert "fantasy teams" in out.lower()

    def test_roster_and_scoring_parse(self):
        team = os.environ.get("WES_YAHOO_TEST_TEAM")
        if not team:
            pytest.skip("set WES_YAHOO_TEST_TEAM=nba.l.<league>.t.<team>")
        league = team.split(".t.")[0]  # nba.l.<league>
        roster = wy.roster(team)
        self._no_error(roster)
        assert "Roster" in roster and "empty" not in roster.lower()
        scoring = wy.league_scoring(league)
        self._no_error(scoring)
        assert "Categories that count" in scoring
