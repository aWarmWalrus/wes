"""Unit tests for the gated executor + action ledger (wes_execute, #029 P3).

STATUS: shadow-mode only — no test here exercises a real Yahoo write, because
none exists yet (see wes_execute.py's module docstring). These tests pin the
guardrail logic, the diff, and the ledger, all of which are real and load-
bearing before any write is built.
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "pc"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import wes_execute as ex  # noqa: E402


class TestDiffLineup:
    def _p(self, name, slot, key="k1", value=None, playing=None):
        return {"name": name, "slot": slot, "player_key": key,
               "value": value, "playing": playing}

    def test_a_starter_who_should_move_is_a_move(self):
        players = [self._p("A", "BN", "1", value=19.3, playing=True)]
        result = {"starters": [{"slot": "QB", "name": "A", "value": 10}],
                  "bench": []}
        moves = ex.diff_lineup(players, result)
        assert moves == [{"player_key": "1", "name": "A",
                          "from_slot": "BN", "to_slot": "QB",
                          "value": 19.3, "playing": True}]

    def test_diff_carries_value_and_playing_for_the_why_summary(self):
        """diff_lineup must pass through value/playing unchanged — this is the
        raw material summarize_moves() explains; if it goes missing, every
        summary silently loses its reasoning."""
        players = [self._p("A", "BN", "1", value=7.5, playing=False)]
        result = {"starters": [{"slot": "QB", "name": "A", "value": 10}],
                  "bench": []}
        moves = ex.diff_lineup(players, result)
        assert moves[0]["value"] == 7.5
        assert moves[0]["playing"] is False

    def test_a_player_already_in_the_right_slot_is_not_a_move(self):
        players = [self._p("A", "QB", "1")]
        result = {"starters": [{"slot": "QB", "name": "A", "value": 10}],
                  "bench": []}
        assert ex.diff_lineup(players, result) == []

    def test_a_bench_recommendation_for_a_current_starter_is_a_move(self):
        players = [self._p("A", "QB", "1", value=5.0, playing=True)]
        result = {"starters": [], "bench": ["A"]}
        moves = ex.diff_lineup(players, result)
        assert moves == [{"player_key": "1", "name": "A",
                          "from_slot": "QB", "to_slot": "BN",
                          "value": 5.0, "playing": True}]

    @pytest.mark.parametrize("alias", ["IR", "IL", "IL+", "BE", "NA"])
    def test_bench_aliases_collapse_so_ir_to_bn_is_not_a_spurious_move(self, alias):
        """Yahoo has several non-starting slots; recommending BN for someone
        already on IR isn't a real move."""
        players = [self._p("A", alias, "1")]
        result = {"starters": [], "bench": ["A"]}
        assert ex.diff_lineup(players, result) == []

    def test_a_player_not_mentioned_by_the_optimizer_is_untouched(self):
        """Only players the RESULT talks about can generate a move — a player
        missing from both starters and bench must not be silently benched."""
        players = [self._p("A", "QB", "1"), self._p("Ghost", "WR", "2")]
        result = {"starters": [{"slot": "QB", "name": "A", "value": 10}],
                  "bench": []}
        assert ex.diff_lineup(players, result) == []

    def test_no_moves_when_lineup_already_matches(self):
        players = [self._p("A", "QB", "1"), self._p("B", "BN", "2")]
        result = {"starters": [{"slot": "QB", "name": "A", "value": 10}],
                  "bench": ["B"]}
        assert ex.diff_lineup(players, result) == []


class TestDomSlot:
    def test_flex_slash_becomes_underscore(self):
        """Empirically confirmed against a real Yahoo flex row 2026-07-30 —
        this is not a guess."""
        assert ex._dom_slot("W/R/T") == "W_R_T"

    def test_single_word_slots_are_unchanged(self):
        assert ex._dom_slot("QB") == "QB"
        assert ex._dom_slot("RB") == "RB"

    def test_bench_aliases_collapse_first(self):
        assert ex._dom_slot("IR") == "BN"


class TestPlanSwaps:
    """Pure planning logic — no browser. This is the layer that has to be
    right before anything clicks a real account: a wrong plan here is a wrong
    write there, with no chance to notice until it's already happened (the
    exact failure recon surfaced: targeting by slot TYPE swapped the wrong
    player when two RB starters existed)."""

    def test_single_move_to_an_empty_slot(self):
        moves = [{"name": "A", "from_slot": "BN", "to_slot": "QB"}]
        current = {"A": "BN"}
        plan = ex._plan_swaps(moves, current)
        assert plan == [("A", None, "QB")]

    def test_a_pair_of_moves_that_satisfy_each_other_becomes_one_swap(self):
        """The exact real scenario: A wants B's slot AND B wants A's — must
        collapse to ONE Yahoo swap, not two independent (and ambiguous) ones."""
        moves = [{"name": "A", "from_slot": "BN", "to_slot": "QB"},
                {"name": "B", "from_slot": "QB", "to_slot": "BN"}]
        current = {"A": "BN", "B": "QB"}
        plan = ex._plan_swaps(moves, current)
        assert plan == [("A", "B", "QB")]   # B satisfied as a side effect

    def test_already_in_place_generates_no_swap(self):
        moves = [{"name": "A", "from_slot": "QB", "to_slot": "QB"}]
        assert ex._plan_swaps(moves, {"A": "QB"}) == []

    def test_two_same_type_targets_pick_the_correct_named_partner(self):
        """The recon bug, reproduced as a plan-level test: two RB starters
        exist (B and C); only B is supposed to leave. The plan must name B as
        the partner, never C — targeting by type alone is exactly what went
        wrong live."""
        moves = [{"name": "A", "from_slot": "BN", "to_slot": "RB"},
                {"name": "B", "from_slot": "RB", "to_slot": "BN"}]
        current = {"A": "BN", "B": "RB", "C": "RB"}   # C stays put
        plan = ex._plan_swaps(moves, current)
        assert plan == [("A", "B", "RB")]
        assert "C" not in [p[0] for p in plan] and "C" not in [p[1] for p in plan]

    def test_flex_target_uses_the_dom_underscore_spelling(self):
        moves = [{"name": "A", "from_slot": "BN", "to_slot": "W/R/T"}]
        plan = ex._plan_swaps(moves, {"A": "BN"})
        assert plan == [("A", None, "W_R_T")]

    def test_bench_alias_current_slot_matches_bn_target(self):
        """A player on IR moving to a plain BN target: IR already normalizes to
        BN, so this must be a no-op, not a spurious swap."""
        moves = [{"name": "A", "from_slot": "IR", "to_slot": "BN"}]
        assert ex._plan_swaps(moves, {"A": "IR"}) == []

    def test_three_way_chain_resolves(self):
        """A -> B's slot, B -> C's slot, C -> A's slot: not a simple pair, but
        must still converge via a bounded sequence of pairwise swaps."""
        moves = [{"name": "A", "from_slot": "QB", "to_slot": "RB"},
                {"name": "B", "from_slot": "RB", "to_slot": "WR"},
                {"name": "C", "from_slot": "WR", "to_slot": "QB"}]
        current = {"A": "QB", "B": "RB", "C": "WR"}
        plan = ex._plan_swaps(moves, current)
        # Simulate the plan against `current` and check the END STATE is right,
        # rather than asserting exact swap order (several valid orders exist).
        sim = dict(current)
        for name, partner, dom_type in plan:
            old = sim[name]
            sim[name] = next(m["to_slot"] for m in moves if m["name"] == name)
            if partner:
                sim[partner] = old
        assert ex._norm_slot(sim["A"]) == "RB"
        assert ex._norm_slot(sim["B"]) == "WR"
        assert ex._norm_slot(sim["C"]) == "QB"

    def test_a_genuinely_impossible_move_is_not_caught_at_planning_time(self):
        """Documented honest limit: _plan_swaps only sees OCCUPIED slots (no
        total-capacity figure), so it can't tell "a real empty slot exists"
        from "every slot of this type is full". It TRUSTS moves came from a
        capacity-valid optimizer run — true in normal use, since diff_lineup
        derives from optimize_lineup against this same roster. B occupying QB
        and staying put makes this move structurally impossible, but planning
        can't see that; it produces a (wrong) best-effort plan targeting an
        assumed empty QB slot."""
        moves = [{"name": "A", "from_slot": "BN", "to_slot": "QB"}]
        current = {"A": "BN", "B": "QB"}
        plan = ex._plan_swaps(moves, current)
        assert plan == [("A", None, "QB")]   # optimistic, and that's the point

    def test_that_same_impossible_case_IS_caught_at_execution_time(self):
        """The safety net one layer down: _execute_swap looks for a REAL empty
        swaptarget row and finds none (only B, who isn't leaving, occupies QB),
        so it raises rather than clicking something wrong. The system as a
        whole still never half-applies silently — planning's blind spot is
        covered by execution's verification."""
        class _Row:
            def query_selector(self, sel):
                return self

            def get_attribute(self, name):
                return "k_a"

            def evaluate_handle(self, *a, **k):
                class _H:
                    def as_element(_self):
                        return self
                return _H()

            def scroll_into_view_if_needed(self):
                pass

            def click(self):
                pass

        class _Page:
            def query_selector(self, sel):
                return _Row()

            def query_selector_all(self, sel):
                return []   # B (staying at QB) never became a swaptarget

            def wait_for_timeout(self, ms):
                pass

            def keyboard(self):
                return self

            def press(self, key):
                pass

        page = _Page()
        page.keyboard = page
        with pytest.raises(RuntimeError):
            ex._execute_swap(page, "A", None, "QB", {"A": "k_a"})

    def test_empty_moves_list_is_an_empty_plan(self):
        assert ex._plan_swaps([], {"A": "QB"}) == []


class TestExecuteSwapTargeting:
    """_execute_swap must match a target by PLAYER NAME text, never by slot
    type alone — a fake DOM with two same-typed swaptargets proves it picks
    the right one."""

    class _El:
        def __init__(self, text, pos, has_label=True):
            self._t, self._pos, self._has_label = text, pos, has_label
            self.clicked = False

        def inner_text(self):
            return self._t

        def get_attribute(self, name):
            return {"data-pos": self._pos}.get(name)

        def query_selector(self, sel):
            return self if self._has_label else None

        def scroll_into_view_if_needed(self):
            pass

        def click(self):
            self.clicked = True

        def evaluate_handle(self, *_a, **_k):
            class _H:
                def as_element(_self):
                    return self
            return _H()

    class _Page:
        def __init__(self, select_owner_row, targets):
            self._row = select_owner_row
            self._targets = targets
            self.escaped = False

        def query_selector(self, sel):
            return self._row if sel.startswith("select") else None

        def query_selector_all(self, sel):
            return self._targets

        def wait_for_timeout(self, ms):
            pass

        def keyboard(self):
            return self

        def press(self, key):
            self.escaped = True

    def test_picks_the_target_matching_the_named_partner(self):
        row = self._El("A's row", "BN")
        b_target = self._El("RB\nPlayer B stats...", "RB")
        c_target = self._El("RB\nPlayer C stats...", "RB")   # same type, wrong player
        page = self._Page(row, [b_target, c_target])
        page.keyboard = page   # simple stand-in

        ex._execute_swap(page, "A", "Player B", "RB", {"A": "k1"})
        assert b_target.clicked is True
        assert c_target.clicked is False

    def test_no_matching_target_raises_and_never_clicks_save(self):
        row = self._El("A's row", "BN")
        wrong = self._El("RB\nSomeone Else", "RB")
        page = self._Page(row, [wrong])
        page.keyboard = page

        with pytest.raises(RuntimeError):
            ex._execute_swap(page, "A", "Player B", "RB", {"A": "k1"})

    def test_missing_player_key_raises(self):
        page = self._Page(None, [])
        with pytest.raises(RuntimeError):
            ex._execute_swap(page, "Ghost", None, "QB", {})


class TestSummarizeMoves:
    """The WHY explanation — pure, reads only value/playing already attached
    to each move by diff_lineup. Grounded in the real numbers from 2026-07-30's
    live verification (Breece Hall 11.85, Cam Skattebo 14.46) so the wording is
    checked against a real scenario, not an invented one."""

    def _m(self, name, from_slot, to_slot, value=None, playing=True, key="k"):
        return {"player_key": key, "name": name, "from_slot": from_slot,
               "to_slot": to_slot, "value": value, "playing": playing}

    def test_a_value_based_swap_reads_as_started_over(self):
        moves = [
            self._m("Cam Skattebo", "BN", "RB", value=14.46, playing=True),
            self._m("Breece Hall", "RB", "BN", value=11.85, playing=True),
        ]
        lines = ex.summarize_moves(moves)
        assert lines == ["Started Cam Skattebo (14.46 pts) at RB over "
                         "Breece Hall (11.85 pts)."]

    def test_an_availability_based_swap_leads_with_the_bye_not_the_value(self):
        """Even if the benched player's raw value looks fine, a bye/no-game is
        the real reason and must be stated as such, not buried."""
        moves = [
            self._m("Jaylen Warren", "BN", "RB", value=12.3, playing=True),
            self._m("Breece Hall", "RB", "BN", value=99.0, playing=False),
        ]
        lines = ex.summarize_moves(moves)
        assert lines == ["Benched Breece Hall (99 pts) (no game this week) "
                         "for Jaylen Warren (12.3 pts) at RB."]

    def test_filling_a_previously_open_slot_has_no_partner(self):
        moves = [self._m("A", "BN", "QB", value=19.3, playing=True)]
        assert ex.summarize_moves(moves) == [
            "Started A (19.3 pts) at QB (the slot was open)."]

    def test_a_lone_bench_move_with_no_replacement(self):
        moves = [self._m("A", "QB", "BN", value=5.0, playing=True)]
        assert ex.summarize_moves(moves) == ["Benched A (5 pts)."]

    def test_a_lone_bench_move_for_a_bye_player(self):
        moves = [self._m("A", "QB", "BN", value=5.0, playing=False)]
        assert ex.summarize_moves(moves) == [
            "Benched A (5 pts) (no game this week)."]

    def test_missing_value_degrades_to_the_name_alone(self):
        """Unknown-value players (see #029's unknown_value note) must still
        produce a readable line, not a crash or a literal 'None pts'."""
        moves = [self._m("A", "BN", "QB", value=None, playing=True)]
        assert ex.summarize_moves(moves) == ["Started A at QB (the slot was open)."]

    def test_multiple_independent_swaps_each_get_their_own_line(self):
        moves = [
            self._m("A", "BN", "QB", value=20.0),
            self._m("B", "QB", "BN", value=10.0),
            self._m("C", "BN", "WR", value=15.0),
            self._m("D", "WR", "BN", value=5.0),
        ]
        lines = ex.summarize_moves(moves)
        assert len(lines) == 2
        assert any("A" in l and "B" in l for l in lines)
        assert any("C" in l and "D" in l for l in lines)

    def test_empty_moves_produces_no_lines(self):
        assert ex.summarize_moves([]) == []


class TestAutonomyFor:
    """Per-action autonomy (2026-08-01). Risk is per action, not per team."""

    def test_scalar_applies_to_every_action(self):
        """Back-compat: the original scalar form still means what it did."""
        t = {"autonomy": "auto"}
        assert ex.autonomy_for(t, "set_lineup") == "auto"
        assert ex.autonomy_for(t, "add_drop") == "auto"

    def test_per_action_map_is_read_per_action(self):
        t = {"autonomy": {"set_lineup": "auto", "add_drop": "propose"}}
        assert ex.autonomy_for(t, "set_lineup") == "auto"
        assert ex.autonomy_for(t, "add_drop") == "propose"

    def test_an_action_absent_from_the_map_is_advise(self):
        """Not mentioning an action has NOT granted it — the safe reading, and
        the one that matters when a new action type is added later."""
        assert ex.autonomy_for({"autonomy": {"set_lineup": "auto"}},
                               "add_drop") == "advise"

    def test_missing_or_junk_autonomy_is_advise(self):
        for t in ({}, {"autonomy": None}, {"autonomy": "banana"},
                  {"autonomy": {"add_drop": "yolo"}}, {"autonomy": 7}):
            assert ex.autonomy_for(t, "add_drop") == "advise"

    def test_case_insensitive(self):
        assert ex.autonomy_for({"autonomy": {"add_drop": "AUTO"}},
                               "add_drop") == "auto"


class TestGuardrails:
    def test_advise_teams_refuse_everything(self):
        allowed, reason = ex.check_guardrails({"autonomy": "advise"}, "set_lineup")
        assert allowed is False
        assert "advise-only" in reason

    def test_action_not_in_allowlist_is_refused(self):
        team = {"autonomy": "propose", "guardrails": {"actions_allowed": []}}
        allowed, reason = ex.check_guardrails(team, "set_lineup")
        assert allowed is False and "actions_allowed" in reason

    def test_allowed_action_under_propose_passes(self):
        team = {"autonomy": "propose",
                "guardrails": {"actions_allowed": ["set_lineup"]}}
        allowed, reason = ex.check_guardrails(team, "set_lineup")
        assert allowed is True and reason == ""

    def test_allowed_action_under_auto_passes(self):
        team = {"autonomy": "auto",
                "guardrails": {"actions_allowed": ["set_lineup", "waiver_claim"]}}
        assert ex.check_guardrails(team, "set_lineup") == (True, "")

    def test_missing_autonomy_defaults_to_advise_and_refuses(self):
        allowed, reason = ex.check_guardrails({}, "set_lineup")
        assert allowed is False

    def test_stale_data_is_refused_per_freshness_guardrail(self):
        team = {"autonomy": "auto",
                "guardrails": {"actions_allowed": ["set_lineup"],
                               "require_fresh_data_minutes": 90},
                "_data_fetched_at": 0.0}
        allowed, reason = ex.check_guardrails(team, "set_lineup", _now=6000.0)
        assert allowed is False and "stale" in reason.lower()

    def test_fresh_data_passes_the_freshness_guardrail(self):
        team = {"autonomy": "auto",
                "guardrails": {"actions_allowed": ["set_lineup"],
                               "require_fresh_data_minutes": 90},
                "_data_fetched_at": 1000.0}
        assert ex.check_guardrails(team, "set_lineup", _now=1060.0) == (True, "")

    def test_no_freshness_requirement_means_no_freshness_check(self):
        team = {"autonomy": "auto",
                "guardrails": {"actions_allowed": ["set_lineup"]}}
        assert ex.check_guardrails(team, "set_lineup", _now=99999.0) == (True, "")


class TestLedger:
    def test_append_writes_one_json_line(self, tmp_path):
        path = str(tmp_path / "ledger.jsonl")
        ex._append_ledger({"a": 1}, path)
        ex._append_ledger({"a": 2}, path)
        lines = [json.loads(l) for l in open(path, encoding="utf-8")]
        assert lines == [{"a": 1}, {"a": 2}]

    def test_creates_the_parent_directory(self, tmp_path):
        path = str(tmp_path / "nested" / "dir" / "ledger.jsonl")
        ex._append_ledger({"a": 1}, path)
        assert os.path.exists(path)

    def test_write_failure_does_not_raise(self, monkeypatch):
        """The ledger is observability, not correctness — a failure to write it
        must never crash the turn that triggered it."""
        def boom(*a, **k):
            raise OSError("disk full")
        monkeypatch.setattr(ex.os, "makedirs", boom)
        ex._append_ledger({"a": 1}, "/should/not/matter")   # must not raise

    def test_default_ledger_path_is_pc_local_not_the_repo(self):
        assert "wes-pc" in ex.LEDGER_FILE
        assert "wes-pc" not in os.path.dirname(os.path.abspath(__file__))


class TestLiveWritesKillSwitch:
    def test_default_is_off(self, monkeypatch):
        monkeypatch.delenv("WES_YAHOO_LIVE_WRITES", raising=False)
        import importlib
        importlib.reload(ex)
        assert ex.LIVE_WRITES is False

    def test_submit_lineup_requires_a_readable_roster_first(self):
        """_submit_lineup now does real work (2026-07-30), but it must refuse
        to even PLAN a write against a roster it couldn't read — never guess
        at a real account's current state."""
        with pytest.raises(RuntimeError):
            ex._submit_lineup("k", [], _roster_fn=lambda k: "couldn't reach Yahoo")


class TestProposeLineupChange:
    RESULT = {"starters": [{"slot": "QB", "name": "Hurts", "value": 19.3}],
             "bench": ["Chase"]}
    PLAYERS = [{"name": "Hurts", "slot": "BN", "player_key": "1"},
              {"name": "Chase", "slot": "WR", "player_key": "2"}]

    def _compute(self, team_key="nfl.l.1.t.1", name="Test", sport="nfl"):
        return lambda team=None: {
            "team_key": team_key, "name": name, "sport": sport,
            "result": self.RESULT, "warn": "", "players": self.PLAYERS}

    def _team(self, monkeypatch, autonomy="propose", allowed=("set_lineup",)):
        monkeypatch.setattr(
            ex.wes_yahoo, "_resolve_team",
            lambda t=None: ({"team_key": "nfl.l.1.t.1", "name": "Test",
                             "autonomy": autonomy,
                             "guardrails": {"actions_allowed": list(allowed)}},
                            None))

    def test_compute_degradation_is_relayed_verbatim(self, monkeypatch):
        self._team(monkeypatch)
        out = ex.propose_lineup_change(
            _compute_fn=lambda team=None: "couldn't reach Yahoo",
            _ledger_path="/dev/null" if os.name != "nt" else None)
        assert out == "couldn't reach Yahoo"

    def test_advise_team_is_told_to_use_the_other_tool(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            ex.wes_yahoo, "_resolve_team",
            lambda t=None: ({"team_key": "x", "name": "Test",
                             "autonomy": "advise"}, None))
        out = ex.propose_lineup_change(
            _compute_fn=self._compute(),
            _ledger_path=str(tmp_path / "l.jsonl"))
        assert "advise-only" in out

    def test_propose_team_gets_a_dry_run_report_naming_the_moves(self, monkeypatch, tmp_path):
        self._team(monkeypatch, autonomy="propose")
        path = tmp_path / "l.jsonl"
        out = ex.propose_lineup_change(_compute_fn=self._compute(),
                                       _ledger_path=str(path))
        assert "Hurts" in out and "Chase" in out
        assert "shadow run" in out.lower()
        logged = json.loads(path.read_text().strip().splitlines()[-1])
        assert logged["dry_run"] is True and logged["executed"] is False
        assert len(logged["moves"]) == 2

    def test_auto_team_without_live_writes_is_a_shadow_run(self, monkeypatch, tmp_path):
        monkeypatch.setattr(ex, "LIVE_WRITES", False)
        self._team(monkeypatch, autonomy="auto")
        out = ex.propose_lineup_change(_compute_fn=self._compute(),
                                       _ledger_path=str(tmp_path / "l.jsonl"))
        assert "auto mode" in out and "live writes are off" in out

    def test_a_write_failure_partway_through_is_reported_honestly(
            self, monkeypatch, tmp_path):
        """When the REAL _submit_lineup (no override) hits an error, the
        message must not claim success AND must not claim nothing happened —
        a mid-plan failure can leave the roster in an intermediate state, so
        the honest thing is to say there was an error and point at Yahoo
        directly. Exercises the real default path via propose_lineup_change,
        with wes_yahoo.roster_players mocked so this stays network-free."""
        monkeypatch.setattr(ex, "LIVE_WRITES", True)
        monkeypatch.setattr(ex.wes_yahoo, "roster_players",
                            lambda k: "couldn't reach Yahoo Fantasy just now.")
        self._team(monkeypatch, autonomy="auto")
        path = tmp_path / "l.jsonl"
        out = ex.propose_lineup_change(_compute_fn=self._compute(),
                                       _ledger_path=str(path))
        assert "error" in out.lower()
        assert "check" in out.lower() or "verify" in out.lower()
        logged = json.loads(path.read_text().strip().splitlines()[-1])
        assert logged["executed"] == "unknown"

    def test_auto_team_with_live_writes_and_a_working_submit_fn_executes(
            self, monkeypatch, tmp_path):
        """The one path where a write WOULD actually happen — proven with an
        injected fake, since no real submit function exists to call for real."""
        monkeypatch.setattr(ex, "LIVE_WRITES", True)
        self._team(monkeypatch, autonomy="auto")
        submitted = []
        path = tmp_path / "l.jsonl"
        out = ex.propose_lineup_change(
            _compute_fn=self._compute(),
            _submit_fn=lambda team_key, moves: submitted.append((team_key, moves)),
            _ledger_path=str(path))
        assert "Set the lineup" in out
        assert len(submitted) == 1 and submitted[0][0] == "nfl.l.1.t.1"
        logged = json.loads(path.read_text().strip().splitlines()[-1])
        assert logged["executed"] is True and logged["dry_run"] is False

    def test_blocked_action_is_logged_and_never_reaches_submit(self, monkeypatch, tmp_path):
        monkeypatch.setattr(ex, "LIVE_WRITES", True)
        self._team(monkeypatch, autonomy="auto", allowed=())   # not allowed
        submitted = []
        path = tmp_path / "l.jsonl"
        out = ex.propose_lineup_change(
            _compute_fn=self._compute(),
            _submit_fn=lambda team_key, moves: submitted.append(1),
            _ledger_path=str(path))
        assert submitted == []
        assert "Not proposing" in out
        logged = json.loads(path.read_text().strip().splitlines()[-1])
        assert logged["allowed"] is False

    def test_no_moves_needed_is_reported_and_logged_without_moves(self, monkeypatch, tmp_path):
        self._team(monkeypatch, autonomy="propose")
        compute = lambda team=None: {   # noqa: E731
            "team_key": "nfl.l.1.t.1", "name": "Test", "sport": "nfl",
            "result": {"starters": [{"slot": "QB", "name": "Hurts", "value": 1}],
                      "bench": []},
            "warn": "",
            "players": [{"name": "Hurts", "slot": "QB", "player_key": "1"}]}
        path = tmp_path / "l.jsonl"
        out = ex.propose_lineup_change(_compute_fn=compute, _ledger_path=str(path))
        assert "already optimal" in out
        logged = json.loads(path.read_text().strip().splitlines()[-1])
        assert logged["moves"] == []

    def test_no_team_configured(self, monkeypatch, tmp_path):
        monkeypatch.setattr(ex.wes_yahoo, "_resolve_team",
                           lambda t=None: (None, None))
        out = ex.propose_lineup_change(
            _compute_fn=self._compute(), _ledger_path=str(tmp_path / "l.jsonl"))
        assert "configured" in out


class TestRecommendRosterMoves:
    """#035 R3 — PURE drop/add recommendation. Every rule here exists to
    prevent a specific bad drop, and a drop is irreversible."""

    def _r(self, name, pos, recent, baseline=None):
        return {"name": name, "positions": [pos], "value": recent,
                "form": {"recent_ppg": recent, "baseline_ppg": baseline}}

    def _a(self, name, pos, value):
        return {"name": name, "positions": [pos], "value": value}

    def test_recommends_a_clear_upgrade(self):
        roster = [self._r("Slumping", "WR", 3.0, 9.0)]
        avail = [self._a("Hot", "WR", 12.0)]
        recs = ex.recommend_roster_moves(roster, avail)
        assert len(recs) == 1
        assert recs[0]["drop"] == "Slumping" and recs[0]["add"] == "Hot"
        assert recs[0]["gain"] == 9.0

    def test_position_must_match(self):
        """Dropping the only kicker for a fourth receiver is strictly worse
        regardless of points — positional need is a hard constraint."""
        roster = [self._r("Kicker", "K", 3.0)]
        avail = [self._a("Receiver", "WR", 20.0)]
        assert ex.recommend_roster_moves(roster, avail) == []

    def test_unknown_form_never_justifies_a_drop(self):
        """recent_form returns None below its games floor. Treating that as 0
        would drop a player for having no data rather than for being bad."""
        roster = [{"name": "Rookie", "positions": ["WR"], "value": None,
                   "form": {"recent_ppg": None, "baseline_ppg": None}}]
        avail = [self._a("Anyone", "WR", 20.0)]
        assert ex.recommend_roster_moves(roster, avail) == []

    def test_unknown_add_value_is_never_recommended(self):
        roster = [self._r("Slumping", "WR", 1.0)]
        avail = [{"name": "Unrated", "positions": ["WR"], "value": None}]
        assert ex.recommend_roster_moves(roster, avail) == []

    def test_marginal_gain_is_not_worth_a_move(self):
        """Churning the roster for +0.3 is noise, and every move spends a
        limited weekly budget."""
        roster = [self._r("Ok", "WR", 10.0)]
        avail = [self._a("Barely", "WR", 10.3)]
        assert ex.recommend_roster_moves(roster, avail) == []
        assert len(ex.recommend_roster_moves(roster, avail, min_gain=0.1)) == 1

    def test_protected_players_are_never_proposed_for_a_drop(self):
        roster = [self._r("Star", "WR", 1.0)]
        avail = [self._a("Hot", "WR", 20.0)]
        assert ex.recommend_roster_moves(roster, avail, protected=["Star"]) == []

    def test_protection_survives_punctuation_differences(self):
        roster = [self._r("Ja'Marr Chase", "WR", 1.0)]
        avail = [self._a("Hot", "WR", 20.0)]
        assert ex.recommend_roster_moves(
            roster, avail, protected=["JaMarr Chase"]) == []

    def test_worst_performer_is_targeted_first(self):
        roster = [self._r("Bad", "WR", 2.0), self._r("Worse", "WR", 1.0)]
        avail = [self._a("A", "WR", 15.0), self._a("B", "WR", 14.0)]
        recs = ex.recommend_roster_moves(roster, avail, limit=2)
        assert recs[0]["drop"] == "Worse"      # biggest gain first
        assert {r["drop"] for r in recs} == {"Bad", "Worse"}

    def test_an_added_player_is_not_proposed_twice(self):
        roster = [self._r("A", "WR", 1.0), self._r("B", "WR", 2.0)]
        avail = [self._a("OnlyGuy", "WR", 20.0)]
        recs = ex.recommend_roster_moves(roster, avail, limit=2)
        assert len(recs) == 1

    def test_limit_is_respected(self):
        roster = [self._r(f"P{i}", "WR", 1.0) for i in range(5)]
        avail = [self._a(f"A{i}", "WR", 20.0) for i in range(5)]
        assert len(ex.recommend_roster_moves(roster, avail, limit=2)) == 2

    def test_empty_inputs_are_empty(self):
        assert ex.recommend_roster_moves([], []) == []
        assert ex.recommend_roster_moves(None, None) == []

    def test_summary_explains_the_move_in_plain_language(self):
        roster = [self._r("Slumping", "WR", 3.0, 9.0)]
        avail = [self._a("Hot", "WR", 12.0)]
        lines = ex.summarize_roster_moves(
            ex.recommend_roster_moves(roster, avail))
        assert "Drop Slumping" in lines[0] and "Hot" in lines[0]
        assert "down from 9" in lines[0]


class TestRosterMoveGuardrails:
    """#035 R4 — the rails that only matter once something IRREVERSIBLE is
    possible. These were declared in teams.yaml and read by no code."""

    def _team(self, **guard):
        return {"team_key": "nfl.l.1.t.1", "autonomy": "auto",
                "guardrails": dict({"actions_allowed": ["add_drop"]}, **guard)}

    def test_never_drop_is_enforced(self):
        ok, why = ex.check_roster_move(
            self._team(never_drop=["Ja'Marr Chase"]), "Ja'Marr Chase")
        assert ok is False and "never_drop" in why

    def test_never_drop_matches_loosely(self):
        """A punctuation difference must not defeat protection."""
        ok, _ = ex.check_roster_move(
            self._team(never_drop=["JaMarr Chase"]), "Ja'Marr Chase")
        assert ok is False

    def test_an_unprotected_player_passes(self):
        ok, why = ex.check_roster_move(
            self._team(never_drop=["Someone Else"]), "Nobody Special")
        assert ok is True and why == ""

    def test_advise_teams_still_refuse(self):
        team = {"team_key": "t", "autonomy": "advise"}
        assert ex.check_roster_move(team, "X")[0] is False

    def test_add_drop_must_be_in_actions_allowed(self):
        team = {"team_key": "t", "autonomy": "auto",
                "guardrails": {"actions_allowed": ["set_lineup"]}}
        ok, why = ex.check_roster_move(team, "X")
        assert ok is False and "actions_allowed" in why

    def test_weekly_cap_blocks_once_reached(self):
        ok, why = ex.check_roster_move(
            self._team(max_moves_per_week=2), "X",
            _count_fn=lambda tk, since, path: 2)
        assert ok is False and "max_moves_per_week" in why

    def test_weekly_cap_allows_below_the_cap(self):
        ok, _ = ex.check_roster_move(
            self._team(max_moves_per_week=4), "X",
            _count_fn=lambda tk, since, path: 1)
        assert ok is True

    def test_an_unreadable_ledger_FAILS_CLOSED(self):
        """The first guardrail depending on HISTORY, not config. If the count
        can't be verified it must refuse — assuming zero moves so far would
        silently allow unlimited drops whenever the ledger is broken."""
        ok, why = ex.check_roster_move(
            self._team(max_moves_per_week=2), "X",
            _count_fn=lambda tk, since, path: None)
        assert ok is False and "couldn't read" in why

    def test_no_cap_configured_means_no_cap_check(self):
        ok, _ = ex.check_roster_move(self._team(), "X",
                                     _count_fn=lambda *a: 999)
        assert ok is True


class TestCountRecentMoves:
    def _e(self, ts, executed=True, action="add_drop", team="nfl.l.1.t.1"):
        return {"ts": ts, "team_key": team, "action_type": action,
                "executed": executed}

    def test_a_corrected_row_is_not_double_counted(self):
        """Regression, 2026-08-07. The ledger is append-only, so correcting a
        row leaves BOTH on disk with executed=True. Counting both inflated the
        weekly total by one and silently ate the budget: two real moves read as
        three, and the live team then refused every roster move for three days
        at a cap it had never reached."""
        original = self._e(100)
        correction = dict(self._e(101), correction_of_ts=100)
        n = ex.count_recent_moves("nfl.l.1.t.1", 0,
                                  _entries_fn=lambda: [original, correction])
        assert n == 1

    def test_a_correction_cancels_a_row_older_than_the_window(self):
        """The superseded row can age out before its correction does; the
        correction must still not be counted as a move in its own right."""
        original = self._e(100)
        correction = dict(self._e(500), correction_of_ts=100)
        n = ex.count_recent_moves("nfl.l.1.t.1", 400,
                                  _entries_fn=lambda: [original, correction])
        assert n == 0

    def test_a_junk_correction_reference_supersedes_nothing(self):
        """Fail toward over-counting, never under-counting — an unparseable
        reference must not silently cancel a real move."""
        entries = [self._e(100), dict(self._e(101), correction_of_ts="oops")]
        assert ex.count_recent_moves("nfl.l.1.t.1", 0,
                                     _entries_fn=lambda: entries) == 2

    def test_counts_only_executed_roster_moves(self):
        entries = [self._e(100), self._e(101, executed=False),
                   self._e(102, action="set_lineup")]
        n = ex.count_recent_moves("nfl.l.1.t.1", 0, _entries_fn=lambda: entries)
        assert n == 1

    def test_uncertain_writes_count_against_the_budget(self):
        """executed="unknown" means it may well have happened, so it must
        consume budget rather than being ignored."""
        entries = [self._e(100, executed="unknown")]
        assert ex.count_recent_moves(
            "nfl.l.1.t.1", 0, _entries_fn=lambda: entries) == 1

    def test_respects_the_time_window_and_team(self):
        entries = [self._e(50), self._e(500), self._e(500, team="other.team")]
        assert ex.count_recent_moves(
            "nfl.l.1.t.1", 100, _entries_fn=lambda: entries) == 1

    def test_missing_ledger_is_zero_not_unknown(self, tmp_path):
        """No ledger file = genuinely no moves yet, which is different from a
        ledger that exists but can't be parsed."""
        assert ex.count_recent_moves("t", 0,
                                     _path=str(tmp_path / "none.jsonl")) == 0

    def test_corrupt_ledger_is_unknown_not_zero(self, tmp_path):
        p = tmp_path / "l.jsonl"
        p.write_text("{not json\n", encoding="utf-8")
        assert ex.count_recent_moves("t", 0, _path=str(p)) is None


class TestIlCandidates:
    """#035 R6 — stashing an injured player on IL frees a bench spot. Only
    worth doing once a freed spot can actually be filled (R5)."""

    SLOTS = ["QB", "RB", "BN", "BN", "IR", "IR"]

    def _p(self, name, slot, status="", key="k"):
        return {"name": name, "slot": slot, "status": status,
                "player_key": key}

    def test_an_injured_bench_player_is_a_candidate(self):
        roster = [self._p("Hurt", "BN", "IR")]
        cands = ex.il_candidates(roster, self.SLOTS)
        assert [c["name"] for c in cands] == ["Hurt"]

    def test_an_injured_starter_is_a_candidate(self):
        roster = [self._p("Hurt", "RB", "O")]
        assert [c["name"] for c in ex.il_candidates(roster, self.SLOTS)] == ["Hurt"]

    def test_a_healthy_player_is_never_a_candidate(self):
        roster = [self._p("Fine", "BN", "")]
        assert ex.il_candidates(roster, self.SLOTS) == []

    def test_questionable_does_not_qualify(self):
        """Q players routinely play and are not IL-eligible — stashing one
        would remove an available player from the lineup pool."""
        roster = [self._p("Maybe", "BN", "Q")]
        assert ex.il_candidates(roster, self.SLOTS) == []

    def test_someone_already_stashed_is_not_re_proposed(self):
        roster = [self._p("Hurt", "IR", "IR")]
        assert ex.il_candidates(roster, self.SLOTS) == []

    def test_no_il_slots_means_no_candidates(self):
        """Stashing needs somewhere to stash."""
        roster = [self._p("Hurt", "BN", "IR")]
        assert ex.il_candidates(roster, ["QB", "RB", "BN"]) == []

    def test_full_il_slots_means_no_candidates(self):
        roster = [self._p("A", "IR", "IR"), self._p("B", "IR", "IR"),
                  self._p("Hurt", "BN", "IR")]
        assert ex.il_candidates(roster, self.SLOTS) == []

    def test_empty_inputs(self):
        assert ex.il_candidates([], self.SLOTS) == []
        assert ex.il_candidates(None, None) == []

    def test_summary_says_what_it_frees(self):
        roster = [self._p("Hurt", "BN", "IR")]
        lines = ex.summarize_il_candidates(ex.il_candidates(roster, self.SLOTS))
        assert "Hurt" in lines[0] and "frees a bench spot" in lines[0]


class TestProposeRosterMoves:
    """The #035 entry point. A write needs either `autonomy.add_drop: auto` or
    an explicit `approve={"drop","add"}` naming the pair — never a bare flag,
    because a flag authorises "whatever is top of the list right now"."""

    APPROVE = {"drop": "Slumping", "add": "Hot"}

    ROSTER = [{"name": "Slumping", "positions": ["WR"], "slot": "BN",
               "status": "", "player_key": "d1"}]
    AVAIL = [{"name": "Hot", "positions": ["WR"], "is_free_agent": True,
              "player_key": "a1"}]
    POOL = [{"name": "Slumping", "positions": ["WR"], "gp": 10,
             "cats": {"RecYds": 300}, "espn_id": "e1"},
            {"name": "Hot", "positions": ["WR"], "gp": 10,
             "cats": {"RecYds": 2000}, "espn_id": "e2"}]

    # Default to add_drop:propose — "recommend, but ask before dropping" is the
    # interesting middle case and the one most tests here are about. Full auto
    # gets its own explicit tests.
    def _cfg(self, monkeypatch, autonomy={"add_drop": "propose"}, **guard):
        monkeypatch.setattr(
            ex.wes_yahoo, "_resolve_team",
            lambda t=None: ({"team_key": "nfl.l.1.t.1", "league_key": "nfl.l.1",
                             "name": "Test", "sport": "nfl",
                             "autonomy": autonomy,
                             "guardrails": dict(
                                 {"actions_allowed": ["add_drop"]}, **guard)},
                            None))

    def _kw(self, tmp_path, **over):
        base = dict(
            _roster_fn=lambda k: self.ROSTER,
            _fa_fn=lambda k: self.AVAIL,
            _pool_fn=lambda: (self.POOL, []),
            _scoring_fn=lambda k: {"weights": ex.wes_nfl.DEFAULT_SCORING,
                                   "tiers": ex.wes_nfl.POINTS_ALLOWED_TIERS},
            _gamelog_fn=lambda aid: [{"cats": {"RecYds": 30}, "date": "2025-01"}] * 5,
            _ledger_path=str(tmp_path / "l.jsonl"))
        base.update(over)
        return base

    def test_recommends_without_executing_by_default(self, monkeypatch, tmp_path):
        self._cfg(monkeypatch)
        out = ex.propose_roster_moves("Test", **self._kw(tmp_path))
        assert "Recommendation only" in out
        assert "Slumping" in out and "Hot" in out

    def test_propose_mode_never_writes_without_an_approval(
            self, monkeypatch, tmp_path):
        """`add_drop: propose` recommends and waits. Nothing about being in
        propose mode may itself trigger the irreversible write."""
        monkeypatch.setattr(ex, "LIVE_WRITES", True)
        self._cfg(monkeypatch, autonomy={"add_drop": "propose"})
        called = []
        out = ex.propose_roster_moves(
            "Test", **self._kw(tmp_path,
                               _submit_fn=lambda *a: called.append(1)))
        assert called == []
        assert "Recommendation only" in out

    def test_execute_respects_never_drop(self, monkeypatch, tmp_path):
        monkeypatch.setattr(ex, "LIVE_WRITES", True)
        self._cfg(monkeypatch, never_drop=["Slumping"])
        called = []
        out = ex.propose_roster_moves(
            "Test", approve=self.APPROVE,
            **self._kw(tmp_path, _submit_fn=lambda *a: called.append(1)))
        # Protected players aren't even proposed, so there's nothing to execute.
        assert called == [] and "No roster moves" in out

    def test_the_cap_no_longer_blocks_an_approved_move(self, monkeypatch,
                                                       tmp_path):
        """REPLACES test_execute_blocked_by_weekly_cap, whose assertion the
        2026-08-07 redesign deliberately inverted. The cap used to stop every
        move; it now bounds UNATTENDED ones, and an approved move is attended.
        Kept as its own test so the change of contract is visible rather than
        just disappearing from the suite."""
        monkeypatch.setattr(ex, "LIVE_WRITES", True)
        monkeypatch.setattr(ex, "count_recent_moves", lambda *a, **k: 99)
        self._cfg(monkeypatch, max_moves_per_week=1)
        called = []
        out = ex.propose_roster_moves(
            "Test", approve=self.APPROVE,
            **self._kw(tmp_path,
                       _submit_fn=lambda tk, a, d: called.append((a, d))))
        assert called == [("a1", "d1")]
        assert "Made this roster move" in out

    def test_execute_with_live_writes_off_is_a_dry_run(self, monkeypatch, tmp_path):
        monkeypatch.setattr(ex, "LIVE_WRITES", False)
        self._cfg(monkeypatch)
        called = []
        out = ex.propose_roster_moves(
            "Test", approve=self.APPROVE,
            **self._kw(tmp_path, _submit_fn=lambda *a: called.append(1)))
        assert called == [] and "live writes are off" in out

    def test_execute_all_gates_open_does_the_move(self, monkeypatch, tmp_path):
        monkeypatch.setattr(ex, "LIVE_WRITES", True)
        monkeypatch.setattr(ex, "count_recent_moves", lambda *a, **k: 0)
        self._cfg(monkeypatch)
        called = []
        path = tmp_path / "l.jsonl"
        out = ex.propose_roster_moves(
            "Test", approve=self.APPROVE,
            **self._kw(tmp_path, _ledger_path=str(path),
                       _submit_fn=lambda tk, a, d: called.append((tk, a, d))))
        assert len(called) == 1
        assert called[0][1] == "a1" and called[0][2] == "d1"   # add/drop keys
        assert "Made this roster move" in out
        logged = json.loads(path.read_text().strip().splitlines()[-1])
        assert logged["executed"] is True

    def test_at_the_cap_auto_degrades_to_propose_instead_of_going_silent(
            self, monkeypatch, tmp_path):
        """Owner design, 2026-08-07: the cap means "stop acting alone", not
        "stop". Before this, hitting the cap refused outright and the team went
        quiet while looking healthy — three real days of that (#035)."""
        monkeypatch.setattr(ex, "LIVE_WRITES", True)
        monkeypatch.setattr(ex, "count_recent_moves", lambda *a, **k: 9)
        self._cfg(monkeypatch, autonomy={"add_drop": "auto"},
                  max_moves_per_week=3)
        called = []
        out = ex.propose_roster_moves(
            "Test", **self._kw(tmp_path, _submit_fn=lambda *a: called.append(1)))
        assert called == []                       # did not act
        assert "Slumping" in out and "Hot" in out  # but still SAID the move
        assert "cap of 3" in out
        assert "suggesting rather than acting" in out

    def test_an_approved_move_goes_through_past_the_cap(self, monkeypatch,
                                                       tmp_path):
        """The cap bounds UNATTENDED moves; an approved one is attended by
        definition, so it is not what the cap exists to stop."""
        monkeypatch.setattr(ex, "LIVE_WRITES", True)
        monkeypatch.setattr(ex, "count_recent_moves", lambda *a, **k: 99)
        self._cfg(monkeypatch, autonomy={"add_drop": "auto"},
                  max_moves_per_week=3)
        called = []
        out = ex.propose_roster_moves(
            "Test", approve=self.APPROVE,
            **self._kw(tmp_path,
                       _submit_fn=lambda tk, a, d: called.append((a, d))))
        assert called == [("a1", "d1")]
        assert "Made this roster move" in out

    def test_an_unreadable_ledger_degrades_rather_than_acting(
            self, monkeypatch, tmp_path):
        """count_recent_moves returns None for UNKNOWN. That must never be read
        as "zero used" and license another unattended write."""
        monkeypatch.setattr(ex, "LIVE_WRITES", True)
        monkeypatch.setattr(ex, "count_recent_moves", lambda *a, **k: None)
        self._cfg(monkeypatch, autonomy={"add_drop": "auto"},
                  max_moves_per_week=3)
        called = []
        out = ex.propose_roster_moves(
            "Test", **self._kw(tmp_path, _submit_fn=lambda *a: called.append(1)))
        assert called == []
        assert "couldn't read the ledger" in out

    def test_the_cap_does_not_soften_a_non_cap_refusal(self, monkeypatch,
                                                      tmp_path):
        """Only the CAP degrades autonomy. never_drop and actions_allowed are
        real refusals and must not be turned into suggestions."""
        monkeypatch.setattr(ex, "LIVE_WRITES", True)
        monkeypatch.setattr(ex, "count_recent_moves", lambda *a, **k: 0)
        self._cfg(monkeypatch, autonomy={"add_drop": "auto"},
                  actions_allowed=["set_lineup"], max_moves_per_week=3)
        called = []
        out = ex.propose_roster_moves(
            "Test", approve=self.APPROVE,
            **self._kw(tmp_path, _submit_fn=lambda *a: called.append(1)))
        assert called == []
        assert "actions_allowed" in out

    def test_full_auto_writes_without_any_approval(self, monkeypatch, tmp_path):
        """`autonomy.add_drop: auto` is TRUE full auto — the scheduled run makes
        the drop unattended. This is the capability the per-action config exists
        to express; before it, autonomy could not say this at all."""
        monkeypatch.setattr(ex, "LIVE_WRITES", True)
        monkeypatch.setattr(ex, "count_recent_moves", lambda *a, **k: 0)
        self._cfg(monkeypatch, autonomy={"add_drop": "auto"})
        called = []
        out = ex.propose_roster_moves(
            "Test", **self._kw(tmp_path,
                               _submit_fn=lambda tk, a, d: called.append((a, d))))
        assert called == [("a1", "d1")]
        assert "Made this roster move" in out

    def test_lineup_auto_does_not_grant_add_drop(self, monkeypatch, tmp_path):
        """The whole point of per-action autonomy: running lineups unattended
        must NOT imply permission to drop anyone."""
        monkeypatch.setattr(ex, "LIVE_WRITES", True)
        self._cfg(monkeypatch, autonomy={"set_lineup": "auto"})
        called = []
        out = ex.propose_roster_moves(
            "Test", **self._kw(tmp_path,
                               _submit_fn=lambda *a: called.append(1)))
        assert called == []
        assert "Recommendation only" in out

    def test_a_stale_approval_is_refused_rather_than_substituted(
            self, monkeypatch, tmp_path):
        """THE reason approval names players instead of being a bare flag. The
        owner approved a pair; by the time it runs, that pair is not what the
        engine would do. It must refuse — not drop whoever is top of the list.
        A cached-empty ESPN page really did shift recs[0] between a suggestion
        and its approval (#035, 2026-07-31)."""
        monkeypatch.setattr(ex, "LIVE_WRITES", True)
        monkeypatch.setattr(ex, "count_recent_moves", lambda *a, **k: 0)
        self._cfg(monkeypatch)
        called = []
        out = ex.propose_roster_moves(
            "Test", approve={"drop": "Someone Else", "add": "Hot"},
            **self._kw(tmp_path, _submit_fn=lambda *a: called.append(1)))
        assert called == []                       # nobody was dropped
        assert "didn't make that move" in out
        assert "Slumping" in out                  # what it WOULD do, to re-ask

    def test_approval_matches_a_lower_ranked_recommendation(
            self, monkeypatch, tmp_path):
        """Approving the second-best move is legitimate — the owner may prefer
        it. It executes THAT pair, not the top one."""
        monkeypatch.setattr(ex, "LIVE_WRITES", True)
        monkeypatch.setattr(ex, "count_recent_moves", lambda *a, **k: 0)
        self._cfg(monkeypatch)
        roster = self.ROSTER + [{"name": "Slumping2", "positions": ["WR"],
                                 "slot": "BN", "status": "",
                                 "player_key": "d2"}]
        avail = self.AVAIL + [{"name": "Hot2", "positions": ["WR"],
                               "is_free_agent": True, "player_key": "a2"}]
        pool = self.POOL + [
            {"name": "Slumping2", "positions": ["WR"], "gp": 10,
             "cats": {"RecYds": 400}, "espn_id": "e3"},
            {"name": "Hot2", "positions": ["WR"], "gp": 10,
             "cats": {"RecYds": 1500}, "espn_id": "e4"}]
        called = []
        out = ex.propose_roster_moves(
            "Test", approve={"drop": "Slumping2", "add": "Hot2"},
            **self._kw(tmp_path, _roster_fn=lambda k: roster,
                       _fa_fn=lambda k: avail, _pool_fn=lambda: (pool, []),
                       _submit_fn=lambda tk, a, d: called.append((a, d))))
        assert called == [("a2", "d2")]
        assert "Made this roster move" in out

    def test_a_half_filled_approval_is_refused(self, monkeypatch, tmp_path):
        monkeypatch.setattr(ex, "LIVE_WRITES", True)
        self._cfg(monkeypatch)
        called = []
        out = ex.propose_roster_moves(
            "Test", approve={"drop": "Slumping"},
            **self._kw(tmp_path, _submit_fn=lambda *a: called.append(1)))
        assert called == [] and "both halves" in out

    def test_approval_tolerates_punctuation_and_case(self, monkeypatch, tmp_path):
        """The name comes back through a 12b, so it round-trips through text.
        Matching is normalized for the same reason never_drop is."""
        monkeypatch.setattr(ex, "LIVE_WRITES", True)
        monkeypatch.setattr(ex, "count_recent_moves", lambda *a, **k: 0)
        self._cfg(monkeypatch)
        called = []
        ex.propose_roster_moves(
            "Test", approve={"drop": "slumping", "add": "HOT"},
            **self._kw(tmp_path,
                       _submit_fn=lambda tk, a, d: called.append((a, d))))
        assert called == [("a1", "d1")]

    def test_only_the_executed_move_is_reported_and_logged(
            self, monkeypatch, tmp_path):
        """Regression, 2026-08-01. Only recs[0] is ever submitted, but the
        report and the ledger were built from the FULL rec list — so a real run
        announced (and recorded as executed) a second drop that never happened.
        The audit trail must match reality; the extras stay visible but are
        labelled as not done."""
        monkeypatch.setattr(ex, "LIVE_WRITES", True)
        monkeypatch.setattr(ex, "count_recent_moves", lambda *a, **k: 0)
        self._cfg(monkeypatch)
        roster = self.ROSTER + [{"name": "Slumping2", "positions": ["WR"],
                                 "slot": "BN", "status": "",
                                 "player_key": "d2"}]
        avail = self.AVAIL + [{"name": "Hot2", "positions": ["WR"],
                               "is_free_agent": True, "player_key": "a2"}]
        pool = self.POOL + [
            {"name": "Slumping2", "positions": ["WR"], "gp": 10,
             "cats": {"RecYds": 400}, "espn_id": "e3"},
            {"name": "Hot2", "positions": ["WR"], "gp": 10,
             "cats": {"RecYds": 1500}, "espn_id": "e4"}]
        path = tmp_path / "l.jsonl"
        called = []
        out = ex.propose_roster_moves(
            "Test", approve=self.APPROVE,
            **self._kw(tmp_path, _roster_fn=lambda k: roster,
                       _fa_fn=lambda k: avail, _pool_fn=lambda: (pool, []),
                       _ledger_path=str(path),
                       _submit_fn=lambda tk, a, d: called.append((a, d))))
        assert len(called) == 1                       # one write, as always
        done = out.split("Also worth considering")[0]
        # The "made" section names ONLY the pair that was actually submitted.
        assert called[0] == ("a1", "d1")
        assert "Slumping2" not in done and "Hot2" not in done
        assert "Also worth considering (not done)" in out
        assert "Slumping2" in out                     # still surfaced, labelled
        logged = json.loads(path.read_text().strip().splitlines()[-1])
        assert logged["executed"] is True
        assert len(logged["moves"]) == 1
        assert logged["moves"][0]["drop"] == "Slumping"

    def test_a_failed_write_is_reported_as_uncertain(self, monkeypatch, tmp_path):
        monkeypatch.setattr(ex, "LIVE_WRITES", True)
        monkeypatch.setattr(ex, "count_recent_moves", lambda *a, **k: 0)
        self._cfg(monkeypatch)
        path = tmp_path / "l.jsonl"

        def boom(tk, a, d):
            raise RuntimeError("yahoo said no")
        out = ex.propose_roster_moves(
            "Test", approve=self.APPROVE,
            **self._kw(tmp_path, _ledger_path=str(path), _submit_fn=boom))
        assert "permanent" in out.lower() and "check the real roster" in out.lower()
        logged = json.loads(path.read_text().strip().splitlines()[-1])
        assert logged["executed"] == "unknown"

    def test_waiver_players_are_not_treated_as_free_pickups(
            self, monkeypatch, tmp_path):
        self._cfg(monkeypatch)
        waiver = [dict(self.AVAIL[0], is_free_agent=False)]
        out = ex.propose_roster_moves(
            "Test", **self._kw(tmp_path, _fa_fn=lambda k: waiver))
        assert "No roster moves" in out

    def test_non_nfl_team_is_refused(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            ex.wes_yahoo, "_resolve_team",
            lambda t=None: ({"team_key": "nba.l.1.t.1", "name": "Hoops",
                             "sport": "nba"}, None))
        out = ex.propose_roster_moves("Hoops", **self._kw(tmp_path))
        assert "NFL-only" in out

    def test_scrape_failures_are_relayed(self, monkeypatch, tmp_path):
        self._cfg(monkeypatch)
        out = ex.propose_roster_moves(
            "Test", **self._kw(tmp_path, _roster_fn=lambda k: "Yahoo is down"))
        assert out == "Yahoo is down"
