"""Judging a pick against consensus, and the banter material built from it.

Roster shapes gave the model exactly one joke — "five RBs and no tight end" —
and it told that joke every time. Having an opinion about a PICK needs to know
what the pick was worth, and that arithmetic is done in code: a 12b model handed
"is pick 14 earlier than consensus rank 40, and by how much" inside a trash-talk
prompt will confidently get it backwards and call a steal a reach.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "pc"))

from sleeper import banter as b  # noqa: E402


class TestPickVerdict:
    def test_a_player_taken_well_before_consensus_is_a_reach(self):
        got = b.pick_verdict(pick_no=14, market_rank=40, teams=6, rounds=15)
        assert got["verdict"] == "reach"
        assert got["rounds_early"] > 0

    def test_a_player_who_falls_is_value(self):
        got = b.pick_verdict(pick_no=50, market_rank=20, teams=12, rounds=15)
        assert got["verdict"] == "steal"
        assert got["rounds_early"] < 0

    def test_a_pick_near_consensus_is_about_right(self):
        got = b.pick_verdict(pick_no=2, market_rank=1, teams=6, rounds=15)
        assert got["verdict"] == "about right"

    def test_rounds_not_picks_is_the_unit(self):
        """Eight picks early means something different in a 6-team league than
        a 12-team one. A round is the unit managers think in."""
        small = b.pick_verdict(20, 32, teams=6, rounds=15)    # 2.0 rounds
        large = b.pick_verdict(20, 32, teams=12, rounds=15)   # 1.0 round
        assert small["rounds_early"] > large["rounds_early"]
        assert small["verdict"] == "reach"
        assert large["verdict"] == "a bit early"

    def test_a_pick_below_the_drafted_pool_is_never_a_reach(self):
        """THE KICKER CASE. Ka'imi Fairbairn went at pick 83 with a market rank
        around 200 — a completely normal round-14 kicker — and the first cut of
        this called it a 'reach, 19.5 rounds early'. Consensus rank stops
        meaning anything past the players a draft will actually take.

        THE ASSERTION CHANGED ON 2026-09-04, and deliberately. This used to
        demand None, which also made every genuinely unranked player invisible
        — including one taken at pick 8 of a live draft (see
        TestUnrankedPlayers). Returning a verdict anchored at pool+1 keeps the
        kicker sane at 1.3 rounds instead of 19.5 while letting the round-one
        reach through.

        What the original test was really protecting is unchanged and is what
        is asserted now: this pick must never be called a reach, and must never
        be notable enough to start a conversation about it."""
        got = b.pick_verdict(83, 200, teams=6, rounds=15)          # pool = 90
        assert got["verdict"] != "reach"
        assert got["verdict"] not in b.NOTABLE_VERDICTS
        assert got["rounds_early"] < 2, "19.5 rounds early was the bug"
        # ...and the same player in a draft deep enough to reach him is judged
        # on his real rank rather than the pool boundary.
        deep = b.pick_verdict(83, 200, teams=16, rounds=15)
        assert deep is not None and deep["market_rank"] == 200
        assert "unranked" not in deep

    def test_a_defence_taken_absurdly_early_is_still_a_reach(self):
        """The pool rule must not become a blanket excuse for K/DEF: a defence
        inside the drafted pool taken in round 5 IS a reach, and saying so is
        the whole point."""
        got = b.pick_verdict(pick_no=30, market_rank=85, teams=6, rounds=15)
        assert got["verdict"] == "reach"

    def test_missing_inputs_still_give_no_verdict(self):
        assert b.pick_verdict(None, 10, teams=6, rounds=15) is None
        assert b.pick_verdict(10, 20, teams=0, rounds=15) is None


class TestUnrankedPlayers:
    """An unranked player taken INSIDE the pool is the biggest reach there is.

    This used to return None, and that blindness was visible in a live draft
    (2026-09-04): one manager took Jjay Mcafee at pick 8, Dakereon Joyner at 32
    and Camerun Peoples at 41 -- nobody had any of them ranked -- and the bot
    said nothing about any of it, because Sleeper returns no search_rank for
    players outside its ranked pool. Across 64 picks the scale produced 38
    value verdicts, 20 "about right", and ZERO reaches. A draft does not behave
    that way; the scale did.

    The old guard bailed on a missing rank AND on a rank past the drafted pool.
    It was written for the opposite case -- a round-14 kicker whose rank of
    ~200 made him "19.5 rounds early" -- and conflating the two cost every real
    reach.
    """

    def test_unranked_taken_in_round_one_is_a_reach(self):
        got = b.pick_verdict(pick_no=8, market_rank=None, teams=12, rounds=15)
        assert got["verdict"] == "reach"
        assert got["unranked"] is True

    def test_unranked_reports_no_rank_rather_than_a_made_up_one(self):
        """`rounds_early` is a FLOOR here -- we know the player is below the
        pool, not where in it. market_rank must stay null so the prompt says
        'nobody had him ranked' instead of quoting a number it cannot support."""
        got = b.pick_verdict(pick_no=32, market_rank=None, teams=12, rounds=15)
        assert got["market_rank"] is None
        assert got["unranked"] is True
        assert got["rounds_early"] > 0

    def test_the_round_14_kicker_stays_sane(self):
        """THE REGRESSION THIS MUST NOT REINTRODUCE. Anchoring at pool+1 rather
        than the raw rank is what keeps him at 1.3 rounds early -- 'a bit
        early', which is not notable and starts no conversation -- instead of
        the 19.5 that made the bot worth muting."""
        got = b.pick_verdict(pick_no=83, market_rank=200, teams=6, rounds=15)
        assert got["verdict"] == "a bit early"
        assert got["rounds_early"] < 2
        assert got["verdict"] not in b.NOTABLE_VERDICTS

    def test_unranked_outside_the_pool_is_still_nothing_to_say(self):
        """Pick 200 of a 180-pick draft: he is below the pool and so is the
        pick. There is no comparison to make."""
        assert b.pick_verdict(200, None, teams=12, rounds=15) is None

    def test_a_ranked_player_is_unaffected(self):
        got = b.pick_verdict(pick_no=50, market_rank=20, teams=12, rounds=15)
        assert got["verdict"] == "steal"
        assert got["market_rank"] == 20
        assert "unranked" not in got

    def test_without_a_pool_size_an_unranked_pick_is_unjudgeable(self):
        """No `rounds` means no pool, so there is no anchor to measure from."""
        assert b.pick_verdict(8, None, teams=12, rounds=None) is None

    def test_the_real_draft_that_exposed_this(self):
        """The three picks from the 2026-09-04 mock, verbatim."""
        for pick_no in (8, 32, 41):
            got = b.pick_verdict(pick_no, None, teams=12, rounds=15)
            assert got and got["verdict"] == "reach", pick_no
            assert got["verdict"] in b.NOTABLE_VERDICTS, "must break silence"


class TestTheBriefTellsItToUseThisMaterial:
    """The prompt is the only place the new fields become behaviour, so it is
    worth one test that they are actually described. A field added to the
    context and never mentioned in the brief is a field the model ignores."""

    def test_the_system_prompt_teaches_the_new_fields(self):
        for token in ("verdict", "rounds_early", "market_rank", "we_wanted"):
            assert token in b.SYSTEM, f"prompt never mentions {token}"

    def test_it_is_told_to_credit_good_picks_not_only_sneer(self):
        low = b.SYSTEM.lower()
        assert "good pick" in low or "give credit" in low

    def test_it_is_told_not_to_recompute_the_verdict(self):
        assert "do not recompute" in b.SYSTEM.lower()

    def test_it_is_told_its_own_picks_carry_a_verdict_too(self):
        """Accused of reaching with the consensus #4 at 1.01, it apologised
        for a pick that was half a round early while holding the rank that
        said so (2026-08-25). The verdict is a defence, not only a weapon."""
        low = b.SYSTEM.lower()
        assert "your own picks carry a verdict" in low
        assert "defence" in low or "defense" in low

    def test_it_is_told_never_to_claim_a_player_it_did_not_draft(self):
        """The ownership paragraph guarded only one direction -- never DISOWN
        your own pick -- so nothing stopped the mirror error. It told the owner
        "you just let Jayden Daniels and Tony Pollard slip by us" when Daniels
        had gone to slot 5 and Pollard to slot 2 (2026-08-25)."""
        low = b.SYSTEM.lower()
        assert "never claim a player you did not draft" in low
        assert "ours: false" in low

    def test_it_is_told_to_own_an_injury_flag_on_its_own_player(self):
        """The documented 2026-08-22 failure -- denying the questionable tag
        on its own first-rounder -- recurred softened on 2026-08-25."""
        assert "lead by owning it" in b.SYSTEM.lower()
