"""Unit tests for the fantasy valuation layer (#029 P1) — network-free.

Covers wes_nba's season-stats parser + name->id resolver (the two things stable
without the live ESPN API, exercised on a saved fixture + injected _get_fn) and
the wes_fantasy engine (formatting + degradation). A live schema-drift check
belongs in a WES_NBA_LIVE-style canary, not here.
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "pc"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import wes_nba as nba  # noqa: E402
import wes_fantasy as fan  # noqa: E402

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures",
                       "espn_athlete_stats.json")
with open(FIXTURE, encoding="utf-8") as _f:
    ATHLETE = json.load(_f)

CATS = ["PTS", "REB", "AST", "ST", "BLK", "TO", "DD", "TD", "EJCT"]


class TestParseSeasonStats:
    def test_parses_latest_season_line(self):
        p = nba.parse_season_stats(ATHLETE, name="Luka Doncic")
        assert p["name"] == "Luka Doncic"
        assert p["season"] == "2025-26"          # newest split, not the first
        assert p["gp"] == 64 and p["min"] == 35.8

    def test_maps_all_league_categories(self):
        cats = nba.parse_season_stats(ATHLETE)["cats"]
        for c in CATS:
            assert c in cats, f"missing {c}"
        assert cats["PTS"] == 33.5 and cats["ST"] == 1.6  # STL -> ST

    def test_counting_cats_are_int_totals(self):
        p = nba.parse_season_stats(ATHLETE)
        assert p["cats"]["DD"] == 34 and isinstance(p["cats"]["DD"], int)
        assert p["counting"] == {"DD", "TD", "EJCT"}

    def test_latest_season_picks_max(self):
        rows = [{"season": {"displayName": "2019-20"}},
                {"season": {"displayName": "2025-26"}},
                {"season": {"displayName": "2023-24"}}]
        assert nba._latest_season(rows) == "2025-26"

    def test_traded_season_takes_max_gp_row(self):
        # a mid-season trade => two rows for one season; take the combined (max GP)
        labels = ["GP", "PTS"]
        rows = [
            {"season": {"displayName": "2025-26"}, "stats": ["20", "10"]},  # team A
            {"season": {"displayName": "2025-26"}, "stats": ["60", "18"]},  # combined
            {"season": {"displayName": "2025-26"}, "stats": ["40", "22"]},  # team B
        ]
        row = nba._row_for(labels, rows, "2025-26")
        assert row["GP"] == "60"

    def test_no_season_returns_none(self):
        assert nba.parse_season_stats({"categories": []}) is None


class TestAthleteId:
    def _search(self, *links):
        return {"results": [{"contents": [{"link": {"web": w}} for w in links]}]}

    def test_extracts_nba_player_id(self):
        data = self._search(
            "https://www.espn.com/nfl/player/_/id/2573402/cam-thomas",
            "https://www.espn.com/nba/player/_/id/4432174/cam-thomas")
        assert nba.athlete_id("Cam Thomas", _get_fn=lambda u: data) == "4432174"

    def test_skips_non_nba_namesakes(self):
        data = self._search(
            "https://www.espn.com/college-football/player/_/id/5154966/cam-thomas")
        assert nba.athlete_id("Cam Thomas", _get_fn=lambda u: data) is None

    def test_degrades_on_error(self):
        def boom(u):
            raise OSError("espn down")
        assert nba.athlete_id("x", _get_fn=boom) is None


class TestPlayerSeasonStats:
    def test_blank_name(self):
        assert "Which player" in nba.player_season_stats("  ")

    def test_unknown_player_degrades(self):
        # search returns no nba link -> no id
        out = nba.player_season_stats("Nobody", _get_fn=lambda u: {"results": []})
        assert isinstance(out, str) and "couldn't find" in out.lower()

    def test_success_returns_dict(self):
        def fake_get(url):
            if "/search/" in url:
                return {"results": [{"contents": [
                    {"link": {"web": "/nba/player/_/id/99/luka"}}]}]}
            return ATHLETE
        out = nba.player_season_stats("Luka", _get_fn=fake_get)
        assert isinstance(out, dict) and out["season"] == "2025-26"


class TestPlayerValue:
    STATS = {"name": "Test Guy", "season": "2025-26", "gp": 70, "min": 30.0,
             "cats": {"PTS": 20.0, "REB": 5.0, "AST": 6.0, "ST": 1.0, "BLK": 0.4,
                      "TO": 2.5, "DD": 10, "TD": 1, "EJCT": 0},
             "counting": {"DD", "TD", "EJCT"}}

    def test_format_shows_all_cats_with_context(self):
        out = fan.format_value(self.STATS, CATS)
        assert out.startswith("Test Guy (2025-26, 70 GP, 30 MPG)")
        assert "PTS 20" in out and "DD 10" in out and "TO 2.5" in out

    def test_missing_cat_renders_dash(self):
        s = dict(self.STATS, cats={"PTS": 20.0})
        assert "REB —" in fan.format_value(s, ["PTS", "REB"])

    def test_versus_compares_two_and_notes_semantics(self):
        seen = []

        def fake(name):
            seen.append(name)
            return dict(self.STATS, name=name)
        out = fan.player_value("A", categories=CATS, versus="B", _stats_fn=fake)
        assert seen == ["A", "B"]
        assert out.count("2025-26") == 2
        assert "lower is better" in out  # TO semantics surfaced

    def test_degradation_passes_through(self):
        out = fan.player_value(
            "X", categories=CATS,
            _stats_fn=lambda n: "I couldn't reach the NBA stats just now.")
        assert out == "I couldn't reach the NBA stats just now."

    def test_default_categories_when_none(self):
        out = fan.player_value("A", _stats_fn=lambda n: self.STATS)
        assert "PTS 20" in out  # falls back to DEFAULT_CATEGORIES


class TestRotoScalar:
    def _s(self, **cats):
        return {"cats": cats}

    def test_turnovers_are_negative(self):
        clean = fan.roto_scalar(self._s(PTS=20.0, TO=0.0), ["PTS", "TO"])
        turnovers = fan.roto_scalar(self._s(PTS=20.0, TO=6.0), ["PTS", "TO"])
        assert turnovers < clean

    def test_spread_normalization_balances_cats(self):
        # 6 REB (spread 3 -> z=2) beats 6 PTS (spread 6 -> z=1) despite equal raw
        pts = fan.roto_scalar(self._s(PTS=6.0), ["PTS"])
        reb = fan.roto_scalar(self._s(REB=6.0), ["REB"])
        assert reb > pts

    def test_percentage_and_missing_cats_skipped(self):
        # FG% is skipped; a missing cat contributes nothing (no crash)
        assert fan.roto_scalar(self._s(PTS=6.0), ["PTS", "FG%", "BLK"]) == 1.0


class TestZScore:
    """Real per-league z-scores (#030) — value relative to the actual pool."""

    def _pool(self):
        return [
            {"name": "Star", "cats": {"PTS": 30.0, "AST": 8.0}, "positions": ["G"]},
            {"name": "Avg", "cats": {"PTS": 20.0, "AST": 5.0}, "positions": ["F"]},
            {"name": "Low", "cats": {"PTS": 10.0, "AST": 2.0}, "positions": ["C"]},
        ]

    def test_ranks_best_player_first(self):
        r = fan.rank_by_zscore(self._pool(), ["PTS", "AST"])
        assert [p["name"] for p in r] == ["Star", "Avg", "Low"]

    def test_mean_player_scores_zero(self):
        # the pool-average player is 0 std devs from the mean in every cat
        r = fan.rank_by_zscore(self._pool(), ["PTS", "AST"])
        avg = next(p for p in r if p["name"] == "Avg")
        assert abs(avg["value"]) < 1e-6

    def test_turnovers_negated(self):
        pool = [{"name": "Clean", "cats": {"PTS": 20.0, "TO": 1.0}},
                {"name": "Sloppy", "cats": {"PTS": 20.0, "TO": 5.0}}]
        r = fan.rank_by_zscore(pool, ["PTS", "TO"])
        assert r[0]["name"] == "Clean"  # fewer turnovers wins the TO category

    def test_scale_free_across_cats(self):
        # a +2σ steals player beats a +1σ points player despite tiny raw steals
        pool = [{"name": "Scorer", "cats": {"PTS": 26.0, "ST": 1.0}},
                {"name": "Thief", "cats": {"PTS": 20.0, "ST": 3.0}},
                {"name": "Base", "cats": {"PTS": 14.0, "ST": 1.0}}]
        r = fan.rank_by_zscore(pool, ["PTS", "ST"])
        assert r[0]["name"] == "Thief"

    def test_baselines_skip_pct_and_thin_cats(self):
        base = fan.category_baselines(self._pool(), ["PTS", "FG%", "BLK"])
        assert "PTS" in base and "FG%" not in base and "BLK" not in base


class TestOptimizeLineup:
    def _p(self, name, pos, val, playing=True, status=""):
        return {"name": name, "positions": pos, "value": val,
                "playing": playing, "status": status}

    def test_fills_eligible_slots_maximizing_value(self):
        players = [self._p("A", ["PG"], 10), self._p("B", ["PG", "SG"], 8),
                   self._p("C", ["C"], 6)]
        r = fan.optimize_lineup(players, ["PG", "G", "C", "BN"])
        got = {s["name"]: s["slot"] for s in r["starters"]}
        assert got == {"A": "PG", "B": "G", "C": "C"}   # A->PG, B->flex G, C->C
        assert r["total"] == 24.0

    def test_benches_injured_and_non_playing(self):
        players = [self._p("Hurt", ["PG"], 99, status="OUT"),
                   self._p("Away", ["PG"], 99, playing=False),
                   self._p("Go", ["PG"], 5)]
        r = fan.optimize_lineup(players, ["PG", "IL"])
        assert [s["name"] for s in r["starters"]] == ["Go"]
        assert set(r["bench"]) == {"Hurt", "Away"}

    def test_greedy_would_be_suboptimal(self):
        # greedy-by-value puts the 10 in the only PG slot and strands nobody for
        # G; the optimum routes the flex player to G so BOTH high scorers start
        players = [self._p("PGonly", ["PG"], 10),
                   self._p("Flex", ["PG", "SG"], 9),
                   self._p("SGonly", ["SG"], 1)]
        r = fan.optimize_lineup(players, ["PG", "G"])
        assert r["total"] == 19.0  # 10 + 9, not 10 + 1

    def test_reports_empty_slots_when_short(self):
        r = fan.optimize_lineup([self._p("A", ["PG"], 5)], ["PG", "C"])
        assert r["empty_slots"] == ["C"]

    def test_zero_value_player_still_starts_over_an_empty_slot(self):
        """Regression: a strict value comparison benched a player worth exactly
        0.0 and reported their slot empty. Caught on the first live NFL run — a
        real DEF sat while the DEF slot showed as unfilled. Zero-value players
        are ordinary (no projection data yet), so filling must win ties."""
        r = fan.optimize_lineup([self._p("Zero", ["C"], 0.0)], ["C", "BN"])
        assert [s["name"] for s in r["starters"]] == ["Zero"]
        assert r["empty_slots"] == []
        assert r["bench"] == []

    def test_unknown_value_is_reported_not_treated_as_zero_silently(self):
        """Regression: 'no stat line found' looked identical to 'worth 0'. A pool
        missing every WR valued Ja'Marr Chase at 0.0, benched him behind a
        replacement-level rookie, and reported it with total confidence."""
        players = [self._p("Elite", ["PG"], None),      # unknown
                   self._p("Known", ["PG"], 3.0)]
        r = fan.optimize_lineup(players, ["PG", "PG", "BN"])
        assert r["unknown_value"] == ["Elite"]
        out = fan.format_lineup(r)
        assert "WARNING" in out and "Elite" in out

    def test_no_warning_when_every_value_is_known(self):
        r = fan.optimize_lineup([self._p("A", ["PG"], 0.0)], ["PG"])
        assert r["unknown_value"] == []
        assert "WARNING" not in fan.format_lineup(r)

    def test_genuine_zero_is_not_flagged_as_unknown(self):
        """0.0 and None must stay distinguishable — a real zero is information."""
        r = fan.optimize_lineup([self._p("Zero", ["PG"], 0.0)], ["PG"])
        assert r["unknown_value"] == []

    def test_tie_break_never_costs_value(self):
        # Only one slot: the 5 must start, not the 0, even though both fill it.
        r = fan.optimize_lineup(
            [self._p("Zero", ["C"], 0.0), self._p("Five", ["C"], 5.0)], ["C"])
        assert [s["name"] for s in r["starters"]] == ["Five"]
        assert r["total"] == 5.0

    @pytest.mark.parametrize("sport,pos_pool,slot_pool", [
        ("nba", ["PG", "SG", "SF", "PF", "C"],
         ["PG", "SG", "G", "SF", "PF", "F", "C", "UTIL"]),
        ("nfl", ["QB", "RB", "WR", "TE", "K", "DEF"],
         ["QB", "RB", "WR", "TE", "W/R", "W/R/T", "Q/W/R/T", "K", "DEF"]),
    ])
    def test_matches_brute_force(self, sport, pos_pool, slot_pool):
        """The DP must equal exhaustive search — for BOTH sports' slot tables,
        since NFL flex slots exercise the same eligibility machinery."""
        import random

        def brute(players, slots):
            active = [s for s in slots if fan._slot_type(s, sport)]
            start = [p for p in players if fan._startable(p, sport)]

            def elig(p, s):
                e = fan._SPORTS[sport]["eligibility"][fan._slot_type(s, sport)]
                return e is None or bool(set(p["positions"]) & e)
            best = [0.0]

            def rec(si, used, tot):
                if si == len(active):
                    best[0] = max(best[0], tot)
                    return
                rec(si + 1, used, tot)
                for j, p in enumerate(start):
                    if j not in used and elig(p, active[si]):
                        rec(si + 1, used | {j}, tot + float(p["value"]))
            rec(0, set(), 0.0)
            return round(best[0], 2)

        rng = random.Random(7)
        for _ in range(1500):
            n = rng.randint(1, 6)
            players = [self._p(f"P{i}", rng.sample(pos_pool, rng.randint(1, 2)),
                               round(rng.uniform(0, 20), 1),
                               playing=rng.random() > 0.2,
                               status=rng.choice(["", "", "O"])) for i in range(n)]
            slots = rng.sample(slot_pool, rng.randint(1, 5)) + ["BN"]
            assert fan.optimize_lineup(players, slots, sport)["total"] \
                == brute(players, slots)


class TestNflLineup:
    """NFL weekly lineups through the same optimizer (#029 P7 pulled forward)."""

    def _p(self, name, pos, val, playing=True, status=""):
        return {"name": name, "positions": pos, "value": val,
                "playing": playing, "status": status}

    NFL_SLOTS = ["QB", "RB", "RB", "WR", "WR", "TE", "W/R/T", "K", "DEF",
                 "BN", "BN", "IR"]

    def test_infers_nfl_from_slots(self):
        assert fan.infer_sport(self.NFL_SLOTS) == "nfl"
        assert fan.infer_sport(["PG", "SG", "C", "UTIL", "BN"]) == "nba"
        assert fan.infer_sport([]) == fan.DEFAULT_SPORT       # no evidence
        assert fan.infer_sport(["BN", "IR"]) == fan.DEFAULT_SPORT

    def test_flex_takes_best_leftover_skill_player(self):
        players = [self._p("Qb", ["QB"], 20), self._p("Rb1", ["RB"], 18),
                   self._p("Rb2", ["RB"], 15), self._p("Rb3", ["RB"], 14),
                   self._p("Wr1", ["WR"], 17), self._p("Wr2", ["WR"], 12),
                   self._p("Te", ["TE"], 9), self._p("K", ["K"], 8),
                   self._p("Def", ["DEF"], 7)]
        r = fan.optimize_lineup(players, self.NFL_SLOTS)
        got = {s["name"]: s["slot"] for s in r["starters"]}
        # Rb3 (14) is the best player with no dedicated slot left -> flex.
        assert got["Rb3"] == "W/R/T"
        assert r["sport"] == "nfl"
        assert r["bench"] == []

    def test_kicker_never_fills_a_skill_slot(self):
        """The regression that the NBA-only table would have allowed: an
        unrecognized/other slot must not become a wildcard for NFL, or a kicker
        ends up at QB. K is eligible for K only."""
        players = [self._p("K", ["K"], 99), self._p("Qb", ["QB"], 1)]
        r = fan.optimize_lineup(players, ["QB", "K"])
        assert {s["name"]: s["slot"] for s in r["starters"]} == \
            {"Qb": "QB", "K": "K"}

    def test_unknown_active_slot_degrades_to_flex_not_wildcard(self):
        players = [self._p("K", ["K"], 99), self._p("Wr", ["WR"], 5)]
        r = fan.optimize_lineup(players, ["WEIRD"], sport="nfl")
        # The flex fallback admits WR/RB/TE — the kicker stays benched.
        assert [s["name"] for s in r["starters"]] == ["Wr"]
        assert r["bench"] == ["K"]

    def test_superflex_admits_a_quarterback(self):
        players = [self._p("Qb1", ["QB"], 20), self._p("Qb2", ["QB"], 19),
                   self._p("Wr", ["WR"], 5)]
        r = fan.optimize_lineup(players, ["QB", "Q/W/R/T"], sport="nfl")
        assert r["total"] == 39.0          # both QBs start, WR benched
        # ...but the plain flex must NOT admit the second QB.
        r2 = fan.optimize_lineup(players, ["QB", "W/R/T"], sport="nfl")
        assert r2["total"] == 25.0         # 20 + the WR's 5

    def test_bye_week_and_doubtful_are_not_startable(self):
        players = [self._p("Bye", ["RB"], 99, playing=False),
                   self._p("Doubtful", ["RB"], 98, status="D"),
                   self._p("Questionable", ["RB"], 10, status="Q"),
                   self._p("Ir", ["RB"], 97, status="IR")]
        r = fan.optimize_lineup(players, ["RB", "BN", "IR"], sport="nfl")
        # Questionable players routinely play, so only they start.
        assert [s["name"] for s in r["starters"]] == ["Questionable"]
        assert set(r["bench"]) == {"Bye", "Doubtful", "Ir"}

    def test_defence_slot_spellings_are_equivalent(self):
        players = [self._p("D", ["DEF"], 7)]
        for label in ("DEF", "D/ST", "DST"):
            r = fan.optimize_lineup(players, [label], sport="nfl")
            assert [s["name"] for s in r["starters"]] == ["D"], label

    def test_format_lineup_says_this_week_for_nfl(self):
        empty = fan.optimize_lineup([], ["BN"], sport="nfl")
        assert "this week" in fan.format_lineup(empty)
        empty_nba = fan.optimize_lineup([], ["BN"], sport="nba")
        assert "today" in fan.format_lineup(empty_nba)

    def test_nba_behaviour_is_unchanged_by_default(self):
        """No sport argument + NBA slots must behave exactly as before."""
        players = [self._p("A", ["PG"], 10), self._p("B", ["PG", "SG"], 8)]
        r = fan.optimize_lineup(players, ["PG", "G", "BN"])
        assert r["sport"] == "nba"
        assert r["total"] == 18.0


class TestOptimizeAssembly:
    ROSTER = [{"name": "Jokic", "positions": ["C"], "slot": "C", "status": "",
               "team": "DEN"},
              {"name": "Curry", "positions": ["PG"], "slot": "PG", "status": "",
               "team": "GS"}]

    def _cfg(self, monkeypatch, team=("t", "Test", "l")):
        key, name, league = team
        monkeypatch.setattr(fan.wes_yahoo, "_resolve_team",
                            lambda t=None: ({"team_key": key, "name": name,
                                             "league_key": league}, None))
        monkeypatch.setattr(fan, "_league_categories", lambda: fan.DEFAULT_CATEGORIES)

    def test_happy_path(self, monkeypatch):
        self._cfg(monkeypatch)
        out = fan.fantasy_optimize_lineup(
            _players_fn=lambda k: self.ROSTER,
            _playing_fn=lambda p: True,
            _value_fn=lambda p: 10.0)
        assert "Optimal lineup" in out and "Jokic" in out and "Curry" in out

    def test_offseason_blank_positions_degrades(self, monkeypatch):
        self._cfg(monkeypatch)
        blank = [{"name": "X", "positions": [], "slot": "PG", "team": ""}]
        out = fan.fantasy_optimize_lineup(_players_fn=lambda k: blank)
        assert "positions" in out and "lineup" in out.lower()

    def test_no_games_today_degrades(self, monkeypatch):
        self._cfg(monkeypatch)
        out = fan.fantasy_optimize_lineup(
            _players_fn=lambda k: self.ROSTER, _playing_fn=lambda p: False)
        assert "no lineup to set" in out.lower() or "no games" in out.lower()

    def test_roster_error_passes_through(self, monkeypatch):
        self._cfg(monkeypatch)
        out = fan.fantasy_optimize_lineup(
            _players_fn=lambda k: "I couldn't reach Yahoo just now.")
        assert out == "I couldn't reach Yahoo just now."

    def test_no_team_configured(self, monkeypatch):
        monkeypatch.setattr(fan.wes_yahoo, "_resolve_team", lambda t=None: (None, None))
        assert "configured" in fan.fantasy_optimize_lineup()

    def test_unfetchable_nba_stats_are_unknown_not_zero(self, monkeypatch):
        """_player_scalar returns None on a failed fetch, so the lineup says so
        rather than ranking the player as worthless."""
        monkeypatch.setattr(fan.wes_nba, "player_season_stats",
                            lambda n: "ESPN is down")
        assert fan._player_scalar({"name": "X"}, fan.DEFAULT_CATEGORIES) is None


class TestNflAssembly:
    """The NFL end of fantasy_optimize_lineup: weekly availability + points."""

    ROSTER = [
        {"name": "Jalen Hurts", "positions": ["QB"], "slot": "QB", "status": "",
         "team": "Phi", "game": "Sun 1:25 pm vs Was"},
        {"name": "Ja'Marr Chase", "positions": ["WR"], "slot": "WR", "status": "",
         "team": "Cin", "game": "Sun 10:00 am vs TB"},
        {"name": "Bye Guy", "positions": ["WR"], "slot": "BN", "status": "",
         "team": "Xxx", "game": "Bye"},
    ]
    SLOTS = ["QB", "WR", "WR", "BN"]

    def _cfg(self, monkeypatch):
        monkeypatch.setattr(
            fan.wes_yahoo, "_resolve_team",
            lambda t=None: ({"team_key": "nfl.l.957011.t.4",
                             "name": "Charles's Pop", "sport": "nfl",
                             "league_key": "nfl.l.957011"}, None))

    def _vmap(self, extra=None):
        base = {fan._norm_name("Jalen Hurts"): 19.3,
                fan._norm_name("Ja'Marr Chase"): 15.7}
        base.update(extra or {})
        return lambda league: (base, [])

    def test_builds_an_nfl_lineup_from_league_slots(self, monkeypatch):
        self._cfg(monkeypatch)
        out = fan.fantasy_optimize_lineup(
            _players_fn=lambda k: self.ROSTER,
            _slots_fn=lambda k: self.SLOTS,
            _valmap_fn=self._vmap())
        assert "Jalen Hurts" in out and "Ja'Marr Chase" in out
        assert "QB:" in out and "WR:" in out

    def test_a_player_on_a_bye_is_not_started(self, monkeypatch):
        self._cfg(monkeypatch)
        out = fan.fantasy_optimize_lineup(
            _players_fn=lambda k: self.ROSTER,
            _slots_fn=lambda k: self.SLOTS,
            _valmap_fn=self._vmap({fan._norm_name("Bye Guy"): 99.0}))
        # Worth the most, but on a bye -> benched, and its WR slot left empty.
        assert "Bye Guy" not in out.split("Bench:")[0]
        assert "Bye Guy" in out

    def test_missing_stats_produce_the_warning(self, monkeypatch):
        self._cfg(monkeypatch)
        out = fan.fantasy_optimize_lineup(
            _players_fn=lambda k: self.ROSTER,
            _slots_fn=lambda k: self.SLOTS,
            _valmap_fn=lambda league: ({}, []))     # nobody has stats
        assert "WARNING" in out and "no stats found" in out

    def test_partial_pool_failure_is_noted(self, monkeypatch):
        self._cfg(monkeypatch)
        out = fan.fantasy_optimize_lineup(
            _players_fn=lambda k: self.ROSTER,
            _slots_fn=lambda k: self.SLOTS,
            _valmap_fn=lambda league: (
                {fan._norm_name("Jalen Hurts"): 19.3}, ["receiving...:desc"]))
        assert "part of the player pool didn't load" in out

    def test_falls_back_to_roster_slots_when_settings_unavailable(self, monkeypatch):
        self._cfg(monkeypatch)
        out = fan.fantasy_optimize_lineup(
            _players_fn=lambda k: self.ROSTER,
            _slots_fn=lambda k: [],                 # settings scrape failed
            _valmap_fn=self._vmap())
        assert "Jalen Hurts" in out                 # still built a lineup

    def test_everyone_on_bye_degrades(self, monkeypatch):
        self._cfg(monkeypatch)
        allbye = [dict(p, game="Bye") for p in self.ROSTER]
        out = fan.fantasy_optimize_lineup(
            _players_fn=lambda k: allbye,
            _slots_fn=lambda k: self.SLOTS,
            _valmap_fn=self._vmap())
        assert "bye" in out.lower() and "no lineup to set" in out.lower()

    def test_sport_inferred_from_team_key_when_config_omits_it(self, monkeypatch):
        monkeypatch.setattr(
            fan.wes_yahoo, "_resolve_team",
            lambda t=None: ({"team_key": "nfl.l.957011.t.4", "name": "X",
                             "league_key": "nfl.l.957011"}, None))
        out = fan.fantasy_optimize_lineup(
            _players_fn=lambda k: self.ROSTER,
            _slots_fn=lambda k: self.SLOTS,
            _valmap_fn=self._vmap())
        assert "Jalen Hurts" in out


class TestNflPlayingThisWeek:
    @pytest.mark.parametrize("game", ["Sun 1:25 pm vs Was", "Thu 5:35 pm @ LAR",
                                      "Mon 5:15 pm vs KC"])
    def test_a_scheduled_game_means_playing(self, game):
        assert fan._nfl_playing({"game": game}) is True

    @pytest.mark.parametrize("game", ["Bye", "BYE", "  bye  ", "Bye Week"])
    def test_a_bye_means_not_playing(self, game):
        assert fan._nfl_playing({"game": game}) is False

    def test_no_game_shown_fails_safe(self):
        """A player we can't confirm must not take a slot from one we can."""
        assert fan._nfl_playing({"game": ""}) is False
        assert fan._nfl_playing({}) is False

    def test_name_normalization_matches_espn_spellings(self):
        assert fan._norm_name("Ja'Marr Chase") == fan._norm_name("JaMarr Chase")
        assert fan._norm_name("James Cook III") == "jamescookiii"
        assert fan._norm_name(None) == ""
