"""The post-compose check: no checkable falsehood reaches the room.

Three separate paragraphs were added to the brief telling the model not to
invent things — about ownership, about its own picks, about the pick it was
handed — and each failure recurred in a later draft. Every rule enforced in
CODE has held. So the last word is a check.

The corpus below is every banter line from the four live mock drafts, verified
against the Sleeper API afterwards. The good lines must survive: a verifier
that silences real material is worse than the failures it prevents.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "pc"))

import wes_banter as b  # noqa: E402


def _ctx(picks=(), roster=(), targets=()):
    return {"recent_picks": list(picks), "our_roster": list(roster),
            "our_targets": list(targets)}


def _pick(no, player, rnd=None, ours=False):
    return {"pick": no, "player": player, "round": rnd, "ours": ours,
            "by": "US" if ours else "slot 4"}


class TestItCatchesTheRealFabrications:
    """Each of these was actually posted to a live draft room."""

    def test_an_invented_pick_number(self):
        """'that Brock Bowers steal at 7' — Bowers went at 39 (2026-08-26)."""
        ctx = _ctx(picks=[_pick(39, "Brock Bowers", rnd=7)])
        why = b.unverifiable(
            "Man, that Brock Bowers steal at 7 is going to haunt me since he "
            "was my top target.", ctx)
        assert why and "39" in why

    def test_claiming_two_players_on_other_teams(self):
        """'you just let Jayden Daniels and Tony Pollard slip by us' — Daniels
        went to slot 5, Pollard to slot 2 (2026-08-25)."""
        ctx = _ctx(picks=[_pick(68, "Jayden Daniels"),
                          _pick(71, "Tony Pollard")],
                   roster=[{"player": "Puka Nacua", "round": 1}])
        why = b.unverifiable("Sorry awarmwalrus, but we took Jayden Daniels "
                             "and Tony Pollard right out from under you.", ctx)
        assert why and "Daniels" in why

    def test_a_player_the_payload_never_mentioned(self):
        ctx = _ctx(picks=[_pick(12, "Rico Dowdle")])
        why = b.unverifiable("Tyreek Hill is going to wreck your season.", ctx)
        assert why and "Tyreek Hill" in why

    def test_an_invented_pick_number_with_any_preposition(self):
        """'Lamar Jackson just fell to pick 23' — he went at 26 (2026-08-28).
        The rule was right; only 'at' was matched, so the phrasing walked past
        it. A check that fires on one wording is a check you cannot rely on."""
        ctx = _ctx(picks=[_pick(26, "Lamar Jackson", rnd=7)])
        for line in ("Lamar Jackson just fell to pick 23 - a steal.",
                     "Lamar Jackson at 23 was a steal.",
                     "Lamar Jackson at pick 23 was a steal.",
                     "They got Lamar Jackson with the 23rd.",
                     "Lamar Jackson to 23 is robbery."):
            assert b.unverifiable(line, ctx), f"missed: {line}"

    def test_projections_and_rates_are_not_pick_numbers(self):
        """A draft room says '255.2 points' and '3.0 YPRR' constantly. None of
        those are pick claims and none may trip the rule."""
        ctx = _ctx(picks=[_pick(26, "Lamar Jackson", rnd=7)])
        for line in ("Lamar Jackson with 255 points projected is fine.",
                     "Lamar Jackson at 23% target share, sure.",
                     "Lamar Jackson to 300 yards a game, apparently."):
            assert b.unverifiable(line, ctx) is None, f"false positive: {line}"

    def test_a_wrong_round(self):
        ctx = _ctx(picks=[_pick(43, "Joe Burrow", rnd=8)])
        why = b.unverifiable("Joe Burrow in the 3rd is an absolute steal.",
                             ctx)
        assert why and "round" in why


class TestItLeavesTheGoodLinesAlone:
    """Every accurate line the bot actually posted. A verifier that eats these
    is worse than the problem — silence is cheap but so is credibility."""

    def test_a_correct_pick_number(self):
        ctx = _ctx(picks=[_pick(53, "Ladd McConkey", rnd=9)])
        assert b.unverifiable(
            "Man, taking Ladd McConkey at 53 was a huge steal; that's exactly "
            "who I was looking for.", ctx) is None

    def test_a_correct_round(self):
        ctx = _ctx(picks=[_pick(43, "Joe Burrow", rnd=8)])
        assert b.unverifiable(
            "Joe Burrow in the 8th is an absolute steal.", ctx) is None

    def test_mourning_a_sniped_target(self):
        ctx = _ctx(picks=[_pick(64, "Rico Dowdle")],
                   targets=[{"name": "Rico Dowdle", "our_rank": 3}])
        assert b.unverifiable(
            "Aw man, Rico Dowdle was right there on our wishlist for this "
            "spot.", ctx) is None

    def test_first_person_framing_about_a_player_we_do_not_own(self):
        """'a huge target FOR US' is honest — we wanted him, we did not claim
        to have him. Possession verbs are the signal, not first person."""
        ctx = _ctx(picks=[_pick(27, "Justin Jefferson")])
        assert b.unverifiable(
            "Aw man, Justin Jefferson was a huge target for us at that spot.",
            ctx) is None

    def test_mocking_another_managers_pick_by_name(self):
        """Naming someone else's player is the job; claiming him is not."""
        ctx = _ctx(picks=[_pick(76, "Jalen Hurts")])
        assert b.unverifiable(
            "At least I'm not settling for Jalen Hurts in the 12th.",
            _ctx(picks=[_pick(76, "Jalen Hurts", rnd=12)])) is None
        assert b.unverifiable("Bold move on Jalen Hurts.", ctx) is None

    def test_talking_about_our_own_roster(self):
        ctx = _ctx(roster=[{"player": "Puka Nacua", "round": 1}])
        assert b.unverifiable(
            "My Puka Nacua pick is going to look fine in December.",
            ctx) is None

    def test_a_line_with_no_player_names(self):
        ctx = _ctx(roster=[{"player": "Puka Nacua", "round": 1}])
        assert b.unverifiable(
            "I'm just making sure my WR corps is elite while your RB room "
            "looks a little crowded.", ctx) is None

    def test_a_sentence_opening_with_two_capitals(self):
        """'Aw man,' and friends must not read as a player name."""
        ctx = _ctx(picks=[_pick(19, "Saquon Barkley")])
        assert b.unverifiable(
            "Aw man, Saquon Barkley was exactly who I wanted at that spot.",
            ctx) is None

    def test_wanting_a_player_is_not_claiming_him(self):
        """'keeping my eyes peeled for Brock Bowers' and 'Odunze is high on my
        radar' — both measured against the live model, both honest lines about
        players we want and do not have. A bare 'my' given any window at all
        fires on them."""
        ctx = _ctx(targets=[{"name": "Brock Bowers"}, {"name": "Rome Odunze"}])
        assert b.unverifiable(
            "I'm keeping my eyes peeled for Brock Bowers, but Rome Odunze is "
            "also high on my radar.", ctx) is None

    def test_a_bare_possessive_flush_against_the_name_is_a_claim(self):
        ctx = _ctx(picks=[_pick(39, "Brock Bowers")])
        assert b.unverifiable("At least my Brock Bowers is healthy.", ctx)

    def test_a_sentence_start_verb_before_a_surname(self):
        """'Snagging Burrow in the 8th' — measured against the live model, and
        a good line: Joe Burrow IS in context and the model referred to him by
        surname. The first cut rejected it because it matched whole candidates
        against whole names."""
        ctx = _ctx(picks=[_pick(43, "Joe Burrow", rnd=8)])
        assert b.unverifiable(
            "Snagging Burrow in the 8th is a massive steal.", ctx) is None

    def test_a_pronoun_before_a_position_acronym(self):
        """'My WR corps is solid' — also a live false positive. Neither word
        is a player-name token, so it is not a player reference at all."""
        ctx = _ctx(roster=[{"player": "CeeDee Lamb", "round": 3}])
        assert b.unverifiable(
            "My WR corps is actually solid, thanks.", ctx) is None

    def test_it_abstains_when_the_player_universe_is_missing(self, monkeypatch):
        """A missing snapshot must not silence a bot that is behaving. The
        invented-name rule stands down; the checkable rules carry on."""
        monkeypatch.setattr(b, "_TOKENS_CACHE", {"v": set()})
        assert b.unverifiable("Tyreek Hill will wreck you.", _ctx()) is None
        # ...but a contradicted pick number is still caught without it.
        ctx = _ctx(picks=[_pick(39, "Brock Bowers")])
        assert b.unverifiable("Brock Bowers at 7 was a steal.", ctx)

    def test_an_empty_line_is_not_a_violation(self):
        assert b.unverifiable("", _ctx()) is None
        assert b.unverifiable(None, _ctx()) is None


class TestTickDropsAndSaysWhy:
    def test_a_fabricated_line_is_dropped_and_logged(self):
        """A dropped message must never look like a quiet turn — the whole
        point is telling 'had nothing to say' from 'made something up'."""
        sent = []
        bt = b.Banter("D", me="us", mode="auto", min_gap_s=0,
                      _now=lambda: 1000.0)
        ctx = {"recent_picks": [dict(_pick(39, "Brock Bowers", rnd=7),
                                     verdict="steal")]}
        bt.tick(context={"recent_picks": []}, _read_fn=lambda _d: [],
                _send_fn=lambda _d, ln: sent.append(ln) or True,
                _post_fn=lambda _b: '{"message": "x"}')          # prime
        act, detail = bt.tick(
            context=ctx, _read_fn=lambda _d: [],
            _send_fn=lambda _d, ln: sent.append(ln) or True,
            _post_fn=lambda _b: '{"message": "Brock Bowers at 7 was a steal"}')
        assert act == "dropped", detail
        assert "UNVERIFIABLE" in detail and "39" in detail
        assert sent == [], "a fabricated line reached the room"

    def test_a_good_line_still_posts(self):
        sent = []
        bt = b.Banter("D", me="us", mode="auto", min_gap_s=0,
                      _now=lambda: 1000.0)
        ctx = {"recent_picks": [dict(_pick(53, "Ladd McConkey", rnd=9),
                                     verdict="steal")]}
        bt.tick(context={"recent_picks": []}, _read_fn=lambda _d: [],
                _send_fn=lambda _d, ln: sent.append(ln) or True,
                _post_fn=lambda _b: '{"message": "x"}')          # prime
        act, _ = bt.tick(
            context=ctx, _read_fn=lambda _d: [],
            _send_fn=lambda _d, ln: sent.append(ln) or True,
            _post_fn=lambda _b: '{"message": "Ladd McConkey at 53, nice"}')
        assert act == "said"
        assert sent == ["Ladd McConkey at 53, nice"]
