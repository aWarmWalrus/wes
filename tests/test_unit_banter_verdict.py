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

import wes_banter as b  # noqa: E402


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

    def test_no_verdict_below_the_drafted_pool(self):
        """THE KICKER CASE. Ka'imi Fairbairn went at pick 83 with a market rank
        around 200 — a completely normal round-14 kicker — and the first cut of
        this called it a 'reach, 19.5 rounds early'. Consensus rank stops
        meaning anything past the players a draft will actually take."""
        assert b.pick_verdict(83, 200, teams=6, rounds=15) is None   # pool = 90
        # ...and the same player in a draft deep enough to reach him is judged.
        assert b.pick_verdict(83, 200, teams=16, rounds=15) is not None

    def test_a_defence_taken_absurdly_early_is_still_a_reach(self):
        """The pool rule must not become a blanket excuse for K/DEF: a defence
        inside the drafted pool taken in round 5 IS a reach, and saying so is
        the whole point."""
        got = b.pick_verdict(pick_no=30, market_rank=85, teams=6, rounds=15)
        assert got["verdict"] == "reach"

    def test_an_unranked_player_gets_no_verdict(self):
        assert b.pick_verdict(10, None, teams=6, rounds=15) is None
        assert b.pick_verdict(None, 10, teams=6, rounds=15) is None
        assert b.pick_verdict(10, 20, teams=0, rounds=15) is None


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
