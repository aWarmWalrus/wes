"""Unit tests for the draft agent (wes_draft_agent, #039).

Network-free and model-free: the model call is injected, so the SAFETY property
is tested rather than the model's taste.
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "pc"))

import wes_draft_agent as agent  # noqa: E402

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
