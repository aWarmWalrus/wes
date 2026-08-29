"""Unit tests for the draft agent (wes_draft_agent, #039).

Network-free and model-free: the model call is injected, so the SAFETY property
is tested rather than the model's taste.
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "pc"))

from sleeper import agent as agent  # noqa: E402

CANDS = [
    {"player_key": "1", "name": "Top Guy", "positions": ["RB"], "team": "SF",
     "vor": 12.0, "need_bump": 4.0, "fit_reasons": []},
    {"player_key": "2", "name": "Second", "positions": ["WR"], "team": "NYJ",
     "vor": 10.0, "need_bump": 4.0, "fit_reasons": []},
    {"player_key": "3", "name": "Third", "positions": ["TE"], "team": "CIN",
     "vor": 8.0, "need_bump": 0.0, "fit_reasons": ["3rd from CIN"]},
]


def _model(reply):
    """A stand-in for the model returning `reply` as its JSON content."""
    return lambda _body: json.dumps(reply)


class TestChoose:
    """THE property. #038's rule is 'the LLM may SUBTRACT, never ADD', which
    works because inaction is safe. Drafting breaks it — a pick is MANDATORY,
    the clock expires and cpu_autopick takes it, so a model that may only veto
    cannot draft. The property is preserved differently: the ENGINE constrains
    the choice set and the MODEL chooses within it."""

    def test_the_model_may_disagree_with_the_board(self):
        """The whole point of an agent rather than a sort — it can take the
        second-ranked player for a reason the ranking does not hold."""
        pick, reason, source = agent.choose(
            CANDS, _post_fn=_model({"player_key": "2", "reason": "WR run"}))
        assert pick["name"] == "Second"
        assert source == "model" and reason == "WR run"

    def test_a_player_NOT_on_the_shortlist_is_refused(self):
        """The load-bearing check. A key that isn't on the shortlist is a
        hallucination, a stale board, or someone just taken — all of which must
        resolve to the engine's pick, never to a guess."""
        pick, reason, source = agent.choose(
            CANDS, _post_fn=_model({"player_key": "999", "reason": "trust me"}))
        assert pick["name"] == "Top Guy"
        assert source == "engine" and "not on the shortlist" in reason

    def test_an_unavailable_model_falls_back_to_the_board(self):
        """cpu_autopick is the real backstop, so a failed model call must
        degrade to a sane pick rather than raising into a live draft."""
        def boom(_body):
            raise OSError("ollama down")
        pick, reason, source = agent.choose(CANDS, _post_fn=boom)
        assert pick["name"] == "Top Guy" and source == "engine"

    def test_unparseable_model_output_falls_back(self):
        pick, _, source = agent.choose(CANDS, _post_fn=lambda _b: "not json")
        assert pick["name"] == "Top Guy" and source == "engine"

    def test_the_source_is_recorded_not_hidden(self):
        """An agent whose judgment silently degrades to a sort is one you
        cannot evaluate later — 'was the model right the twelve times it
        disagreed?' has to stay answerable."""
        _, _, model_src = agent.choose(
            CANDS, _post_fn=_model({"player_key": "3", "reason": "need TE"}))
        _, _, engine_src = agent.choose(CANDS, _post_fn=lambda _b: "junk")
        assert model_src == "model" and engine_src == "engine"

    def test_a_missing_reason_does_not_produce_an_empty_explanation(self):
        _, reason, _ = agent.choose(
            CANDS, _post_fn=_model({"player_key": "2"}))
        assert reason.strip()

    def test_no_candidates_is_handled(self):
        pick, _, _ = agent.choose([], _post_fn=_model({"player_key": "1"}))
        assert pick is None

    def test_the_shortlist_sent_to_the_model_carries_fit_concerns(self):
        """The model can only weigh what it is shown; dropping the fit reasons
        would leave it re-deriving roster construction it cannot see."""
        seen = {}

        def capture(body):
            seen["payload"] = json.loads(body)
            return json.dumps({"player_key": "1", "reason": "ok"})
        agent.choose(CANDS, _post_fn=capture)
        sent = json.loads(seen["payload"]["messages"][1]["content"])
        third = next(c for c in sent["shortlist"] if c["player_key"] == "3")
        assert third["fit_concerns"] == ["3rd from CIN"]
        assert third["value_over_replacement"] == 8.0


class TestDecide:
    def test_degradation_from_the_board_is_relayed_verbatim(self):
        out = agent.decide("L", "D", 1,
                           _board_fn=lambda *a, **k: "That draft is over.")
        assert out == "That draft is over."

    def test_engine_fallback_is_visible_in_the_reply(self):
        """If the agent quietly stopped using judgment, the reply should say
        so rather than looking like a considered pick."""
        out = agent.decide(
            "L", "D", 1,
            _board_fn=lambda *a, **k: {"candidates": CANDS},
            _post_fn=lambda _b: "junk")
        assert "Top Guy" in out and "engine fallback" in out

    def test_a_model_pick_reads_as_a_decision(self):
        out = agent.decide(
            "L", "D", 1,
            _board_fn=lambda *a, **k: {"candidates": CANDS},
            _post_fn=_model({"player_key": "2", "reason": "WR run starting"}))
        assert "Second" in out and "WR run starting" in out
        assert "engine fallback" not in out


class TestFencedOutput:
    """Found by the replay harness, 2026-08-15. Ollama's format=json guarantees
    a bare object; the Anthropic API does not, and Claude wraps its reply in a
    markdown fence. Every Claude call parsed as garbage and was reported as
    'model unavailable' — a misleading diagnosis, since it had answered fine."""

    def test_a_fenced_reply_is_parsed(self):
        fenced = '```json\n{"player_key": "2", "reason": "WR run"}\n```'
        pick, reason, source = agent.choose(CANDS, _post_fn=lambda _b: fenced)
        assert pick["name"] == "Second" and source == "model"

    def test_a_bare_reply_still_works(self):
        pick, _, source = agent.choose(
            CANDS, _post_fn=_model({"player_key": "2", "reason": "x"}))
        assert pick["name"] == "Second" and source == "model"

    def test_a_fence_without_the_json_tag(self):
        fenced = '```\n{"player_key": "3", "reason": "need TE"}\n```'
        pick, _, source = agent.choose(CANDS, _post_fn=lambda _b: fenced)
        assert pick["name"] == "Third" and source == "model"

    def test_genuine_garbage_still_falls_back_with_an_honest_reason(self):
        _, reason, source = agent.choose(CANDS, _post_fn=lambda _b: "not json")
        assert source == "engine" and "no usable reply" in reason


class TestContextReachesTheModel:
    """The `context` parameter existed from the start and the loop never
    populated it, so the model saw eight players with numbers and no idea what
    was already on the roster. It took nine running backs and no quarterback
    (2026-08-15, full mock) — and could not have known better."""

    def test_context_is_actually_sent(self):
        seen = {}

        def capture(body):
            seen["payload"] = json.loads(body)
            return json.dumps({"player_key": "1", "reason": "ok"})
        ctx = {"round": 4, "still_unfilled": {"QB": 1, "TE": 1},
               "roster_so_far": [{"name": "A Back", "position": "RB"}]}
        agent.choose(CANDS, context=ctx, _post_fn=capture)
        sent = json.loads(seen["payload"]["messages"][1]["content"])
        assert sent["context"]["still_unfilled"] == {"QB": 1, "TE": 1}
        assert sent["context"]["roster_so_far"][0]["position"] == "RB"

    def test_the_prompt_tells_the_model_what_to_do_with_it(self):
        """Context nobody is told to use is decoration."""
        assert "starting slot" in agent.SYSTEM.lower()
        assert "current roster" in agent.SYSTEM.lower()

    def test_absent_context_still_works(self):
        pick, _, source = agent.choose(
            CANDS, _post_fn=_model({"player_key": "2", "reason": "x"}))
        assert pick["name"] == "Second" and source == "model"


class TestExplain:
    """Explaining is a SECOND call, made after the pick is fixed. Asking for
    the rationale in the same breath as the decision measurably damaged the
    decision: five variants, none better than the baseline, one that lost the
    quarterback, the kicker AND the defence (2026-08-15)."""

    CANDS = [{"player_key": "1", "name": "Alpha", "positions": ["RB"],
              "vor": 5.0, "handcuff_for": "Our Starter"},
             {"player_key": "2", "name": "Beta", "positions": ["WR"],
              "vor": 4.0}]

    def _reply(self, obj):
        return lambda _b: json.dumps(obj)

    def test_it_records_the_factors_and_the_runner_up(self):
        got = agent.explain(self.CANDS, self.CANDS[0], _post_fn=self._reply({
            "considered": ["value over replacement 5.0", "fills the RB slot"],
            "runner_up": "2", "why_not": "already stacked at WR"}))
        considered, runner, why = got
        assert considered == ["value over replacement 5.0",
                              "fills the RB slot"]
        assert runner == "Beta" and why == "already stacked at WR"

    def test_the_runner_up_resolves_to_a_NAME_not_the_key(self):
        _c, runner, _w = agent.explain(
            self.CANDS, self.CANDS[0], _post_fn=self._reply({"runner_up": "2"}))
        assert runner == "Beta"

    def test_a_runner_up_off_the_shortlist_is_dropped(self):
        """Same rule as the pick: a key we cannot resolve is a hallucination,
        and logging it would name a player who was never considered."""
        _c, runner, _w = agent.explain(
            self.CANDS, self.CANDS[0],
            _post_fn=self._reply({"runner_up": "999"}))
        assert runner is None

    def test_the_chosen_player_cannot_be_his_own_runner_up(self):
        _c, runner, _w = agent.explain(
            self.CANDS, self.CANDS[0], _post_fn=self._reply({"runner_up": "1"}))
        assert runner is None

    def test_a_dead_model_degrades_to_empty_rather_than_raising(self):
        """The pick is already made; a failed explanation must cost nothing."""
        assert agent.explain(self.CANDS, self.CANDS[0],
                             _post_fn=lambda _b: "not json") == ([], None, "")

    def test_junk_in_considered_does_not_raise(self):
        considered, _r, _w = agent.explain(
            self.CANDS, self.CANDS[0],
            _post_fn=self._reply({"considered": "not a list"}))
        assert considered == []

    def test_it_is_shown_the_handcuff_the_pick_call_never_sees(self):
        """handcuff_for is deliberately absent from the PICK payload -- it
        measured worse there -- but the explanation can afford it."""
        seen = {}

        def capture(body):
            seen.update(json.loads(body))
            return "{}"
        agent.explain(self.CANDS, self.CANDS[0], _post_fn=capture)
        sent = json.loads(seen["messages"][1]["content"])
        assert sent["shortlist"][0]["handcuff_for"] == "Our Starter"
        assert sent["player_taken"] == "1"

    def test_the_pick_payload_still_has_no_handcuff(self):
        """The frozen contract. If this fails, someone has re-added a field to
        the measured-good pick call -- re-run tests/draft_replay.py first."""
        assert "handcuff_for" not in agent._entry(self.CANDS[0])
        assert "depth_chart_order" not in agent._entry(self.CANDS[0])

    def test_explaining_nothing_is_not_an_error(self):
        assert agent.explain(self.CANDS, None) == ([], None, "")


class TestDecideOne:
    CANDS = TestExplain.CANDS

    def test_it_attaches_the_explanation_to_the_record(self):
        d = agent.decide_one(
            self.CANDS,
            _post_fn=lambda _b: json.dumps({"player_key": "1",
                                            "reason": "best back"}),
            _explain_post_fn=lambda _b: json.dumps(
                {"considered": ["VOR 5.0"], "runner_up": "2",
                 "why_not": "less value"}))
        assert d["candidate"]["name"] == "Alpha"
        assert d["reason"] == "best back"
        assert d["considered"] == ["VOR 5.0"] and d["runner_up"] == "Beta"

    def test_with_explanation_false_makes_no_second_call(self):
        """On a tight clock a pick is worth more than a paragraph about it."""
        calls = []

        def count(_b):
            calls.append(1)
            return json.dumps({"player_key": "1"})
        agent.decide_one(self.CANDS, _post_fn=count, _explain_post_fn=count,
                         with_explanation=False)
        assert len(calls) == 1

    def test_an_engine_fallback_is_explained_too(self):
        """A fallback pick still deserves a record of why it stands."""
        d = agent.decide_one(
            self.CANDS, _post_fn=lambda _b: "not json",
            _explain_post_fn=lambda _b: json.dumps({"considered": ["top VOR"]}))
        assert d["source"] == "engine"
        assert d["considered"] == ["top VOR"]

    def test_choose_never_pays_for_an_explanation(self):
        """choose() discards the detail, so a second round trip would be pure
        latency."""
        calls = []

        def count(_b):
            calls.append(1)
            return json.dumps({"player_key": "1", "reason": "r"})
        agent.choose(self.CANDS, _post_fn=count)
        assert len(calls) == 1


class TestFormatDecision:
    def test_it_lays_the_factors_out_one_per_line(self):
        """A rationale squeezed onto one line is a rationale nobody reads."""
        txt = agent.format_decision({
            "candidate": {"name": "Alpha"}, "reason": "best back",
            "source": "model", "considered": ["VOR 5.0", "RB run"],
            "runner_up": "Beta", "why_not": "stacked at WR"})
        assert "Alpha (model: best back)" in txt
        assert "weighed: VOR 5.0" in txt and "weighed: RB run" in txt
        assert "Beta" in txt and "stacked at WR" in txt

    def test_a_bare_decision_still_formats(self):
        txt = agent.format_decision({
            "candidate": {"name": "Alpha"}, "reason": "r", "source": "engine",
            "considered": [], "runner_up": None, "why_not": ""})
        assert txt == "Alpha (engine: r)"

    def test_no_candidate_reports_the_reason(self):
        assert agent.format_decision(
            {"candidate": None, "reason": "no candidates"}) == "no candidates"
