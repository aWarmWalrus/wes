"""When the banter is allowed to speak unprompted.

It used to fire only on a human typing, so a draft where nobody chatted
produced nothing at all no matter what happened on the board — yesterday's mock
managed one message across ninety picks. The material worth reacting to arrives
as PICKS, so picks are a trigger too, behind the same rate limiter.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "pc"))

import wes_banter as b  # noqa: E402


def _pick(no, **kw):
    row = {"pick": no, "by": "rival", "ours": False, "player": f"P{no}",
           "position": "RB"}
    row.update(kw)
    return row


class TestNotablePicks:
    def test_a_sniped_target_is_notable(self):
        got = b.notable_picks([_pick(10, we_wanted=True)], set())
        assert [p["pick"] for p in got] == [10]

    def test_a_reach_and_a_steal_are_notable(self):
        got = b.notable_picks(
            [_pick(1, verdict="reach"), _pick(2, verdict="steal")], set())
        assert [p["pick"] for p in got] == [1, 2]

    def test_minor_verdicts_are_not(self):
        """A bot narrating every mild opinion is the running commentary nobody
        asked for."""
        picks = [_pick(1, verdict="about right"),
                 _pick(2, verdict="a bit early"),
                 _pick(3, verdict="good value")]
        assert b.notable_picks(picks, set()) == []

    def test_our_own_picks_are_never_notable(self):
        """'Happy with that one' after every selection is the fastest route to
        being muted, and it is the trigger most likely to annoy."""
        picks = [_pick(5, ours=True, verdict="steal"),
                 _pick(6, ours=True, we_wanted=True)]
        assert b.notable_picks(picks, set()) == []

    def test_already_seen_picks_do_not_re_fire(self):
        picks = [_pick(10, verdict="reach")]
        assert b.notable_picks(picks, {10}) == []


class TestTickTriggers:
    def _banter(self, min_gap_s=0):
        return b.Banter("D", me="us", mode="auto", min_gap_s=min_gap_s,
                        _now=lambda: 1000.0)

    def _run(self, bt, msgs, context, sent):
        return bt.tick(context=context,
                       _read_fn=lambda _d: msgs,
                       _send_fn=lambda _d, line: sent.append(line) or True,
                       _post_fn=lambda _body: '{"message": "a line"}')

    def test_the_first_pass_primes_and_says_nothing(self):
        """Arriving mid-draft must not produce a burst of commentary about
        picks that happened before we got here."""
        bt, sent = self._banter(), []
        act, detail = self._run(
            bt, [], {"recent_picks": [_pick(1, verdict="reach")]}, sent)
        assert act == "quiet" and "primed" in detail
        assert sent == []

    def test_a_new_notable_pick_speaks_with_nobody_talking(self):
        bt, sent = self._banter(), []
        self._run(bt, [], {"recent_picks": []}, sent)          # prime
        act, detail = self._run(
            bt, [], {"recent_picks": [_pick(9, we_wanted=True)]}, sent)
        assert act == "said", detail
        assert sent == ["a line"]
        assert "WE WANTED HIM" in detail

    def test_an_unremarkable_pick_stays_quiet(self):
        bt, sent = self._banter(), []
        self._run(bt, [], {"recent_picks": []}, sent)
        act, _ = self._run(
            bt, [], {"recent_picks": [_pick(9, verdict="about right")]}, sent)
        assert act == "quiet"
        assert sent == []

    def test_the_same_pick_does_not_fire_twice(self):
        bt, sent = self._banter(), []
        self._run(bt, [], {"recent_picks": []}, sent)
        ctx = {"recent_picks": [_pick(9, verdict="steal")]}
        assert self._run(bt, [], ctx, sent)[0] == "said"
        assert self._run(bt, [], ctx, sent)[0] == "quiet"
        assert len(sent) == 1

    def test_chat_wins_when_both_fire(self):
        """Somebody spoke to us; answering that matters more than remarking on
        the board, and the picks are in context either way."""
        bt, sent = self._banter(), []
        self._run(bt, [], {"recent_picks": []}, sent)
        act, detail = self._run(
            bt, [{"author": "rival", "text": "hey you", "system": False}],
            {"recent_picks": [_pick(9, verdict="steal")]}, sent)
        assert act == "said"
        assert "rival: hey you" in detail

    def test_pick_triggers_obey_the_rate_limit(self):
        """The limiter is the only volume control now that the prompt no
        longer asks for restraint — picks arrive far faster than chat."""
        bt, sent = self._banter(min_gap_s=180.0), []
        self._run(bt, [], {"recent_picks": []}, sent)
        assert self._run(
            bt, [], {"recent_picks": [_pick(9, verdict="steal")]}, sent)[0] \
            == "said"
        act, _ = self._run(
            bt, [], {"recent_picks": [_pick(10, verdict="reach")]}, sent)
        assert act == "rate_limited"
        assert len(sent) == 1


class TestWhichPickItReactsTo:
    """When several picks qualify at once, only one gets the line."""

    def test_a_sniped_target_outranks_a_mere_steal(self):
        """Observed live: pick 42 took a player off our shortlist and pick 43
        was somebody else's steal, and it remarked on the steal (2026-08-24).
        Taking the most recent handed the model the less interesting one."""
        got = b._worth_most([_pick(42, we_wanted=True),
                             _pick(43, verdict="steal")])
        assert got["pick"] == 42

    def test_otherwise_the_most_recent_wins(self):
        got = b._worth_most([_pick(1, verdict="reach"),
                             _pick(2, verdict="steal")])
        assert got["pick"] == 2

    def test_the_latest_snipe_wins_among_snipes(self):
        got = b._worth_most([_pick(1, we_wanted=True),
                             _pick(2, we_wanted=True)])
        assert got["pick"] == 2

    def test_nothing_to_choose_from_is_None(self):
        assert b._worth_most([]) is None


class TestComposeGuards:
    def test_nothing_to_react_to_is_None(self):
        assert b.compose(None, context={}, reacting_to=None) is None

    def test_a_pick_alone_is_enough_to_compose(self):
        got = b.compose(None, context={}, reacting_to=_pick(1, verdict="reach"),
                        _post_fn=lambda _b: '{"message": "hi"}')
        assert got == "hi"
