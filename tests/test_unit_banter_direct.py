"""Being spoken to is different from being spoken near.

A question aimed at the room and a question aimed at you are different acts,
and answering the second slowly reads as broken rather than restrained — asked
"what happened bro" after losing a pick, this said nothing for a whole poll
cycle (2026-08-21). A message that names us gets the shorter floor.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "pc"))

import wes_banter as b  # noqa: E402

ME = "GMBartimusPrime"


def _m(text, author="GMSnappy"):
    return {"author": author, "text": text, "system": False}


class TestNameDetection:
    def test_the_full_display_name(self):
        assert b.addressed_to([_m("GMBartimusPrime you are cooked")], ME)

    def test_the_short_form_people_actually_type(self):
        """Nobody types GMBartimusPrime twice. They shorten it."""
        for text in ("bartimus what are you doing",
                     "Bartimus, that was a reach",
                     "@bartimus lol",
                     "hey Bartimus?"):
            assert b.addressed_to([_m(text)], ME), text

    def test_punctuation_and_case_do_not_matter(self):
        assert b.addressed_to([_m("G.M. Bartimus-Prime!!")], ME)

    def test_ordinary_chat_is_not_addressed(self):
        for text in ("who you taking?", "that's a reach",
                     "GMSnappy is up next", "prime time baby",
                     "gm everyone"):
            assert not b.addressed_to([_m(text)], ME), text

    def test_only_the_longest_component_counts(self):
        """Taking every component of five-plus characters also accepted
        'prime', and 'prime time baby' is a thing people say about players --
        it would have jumped the queue as if somebody had asked a question."""
        assert not b.addressed_to([_m("gm all")], ME)
        assert not b.addressed_to([_m("prime pick right there")], ME)
        assert not b.addressed_to([_m("prime time baby")], ME)
        assert b.addressed_to([_m("bartimus?")], ME)

    def test_it_works_for_other_managers_names_too(self):
        """Not special-cased to one account -- -User can point this anywhere."""
        assert b.addressed_to([_m("snappy that's a reach")], "GMSnappy")
        assert b.addressed_to([_m("awarmwalrus you up?")], "awarmwalrus")
        assert not b.addressed_to([_m("gm folks")], "GMSnappy")

    def test_it_returns_the_matching_messages(self):
        msgs = [_m("who's up"), _m("bartimus your turn"), _m("nice")]
        got = b.addressed_to(msgs, ME)
        assert [m["text"] for m in got] == ["bartimus your turn"]

    def test_no_name_means_nothing_is_addressed(self):
        assert b.addressed_to([_m("hello")], "") == []
        assert b.addressed_to(None, ME) == []


class TestTheShorterFloor:
    def _bt(self, **kw):
        kw.setdefault("min_gap_s", 60.0)
        kw.setdefault("direct_gap_s", 30.0)
        return b.Banter("D", me=ME, mode="auto", _now=kw.pop("_now"), **kw)

    def _tick(self, bt, msgs, reply="ok"):
        return bt.tick(context={"recent_picks": []},
                       _read_fn=lambda _d: msgs,
                       _send_fn=lambda _d, ln: True,
                       _post_fn=lambda _b: '{"message": "%s"}' % reply)

    def test_a_direct_message_beats_the_general_floor(self):
        clock = {"t": 1000.0}
        bt = self._bt(_now=lambda: clock["t"])
        self._tick(bt, [])                                   # prime
        assert self._tick(bt, [_m("first")])[0] == "said"
        clock["t"] += 40                                     # 40s: past 30, not 60
        act, _ = self._tick(bt, [_m("bartimus you there?")])
        assert act == "said", "a direct question waited on the general floor"

    def test_ordinary_chatter_still_waits_the_full_floor(self):
        clock = {"t": 1000.0}
        bt = self._bt(_now=lambda: clock["t"])
        self._tick(bt, [])
        assert self._tick(bt, [_m("first")])[0] == "said"
        clock["t"] += 40
        act, detail = self._tick(bt, [_m("anyway who's next")])
        assert act == "rate_limited" and "(direct)" not in detail

    def test_a_direct_message_is_still_rate_limited(self):
        """A floor, not an exemption -- somebody spamming our name cannot turn
        this into a chat client."""
        clock = {"t": 1000.0}
        bt = self._bt(_now=lambda: clock["t"])
        self._tick(bt, [])
        self._tick(bt, [_m("first")])
        clock["t"] += 5
        act, detail = self._tick(bt, [_m("bartimus?")])
        assert act == "rate_limited" and "(direct)" in detail

    def test_the_direct_gap_never_exceeds_the_general_one(self):
        """Widening the gap to quieten the bot must not make it chattier when
        spoken to."""
        bt = b.Banter("D", me=ME, min_gap_s=10.0, direct_gap_s=30.0)
        assert bt.direct_gap_s == 10.0


class TestThePayloadSaysSoToo:
    def test_addressed_messages_are_flagged_for_the_model(self):
        """The model cannot spot this itself -- the full name, a short form and
        a bare question all look like chat to it."""
        msgs = [_m("who's up"), _m("bartimus explain yourself")]
        direct = b.addressed_to(msgs, ME)
        payload = b.build_payload(msgs, {"round": 3}, direct=direct)
        assert "addressed_to_you" in payload
        assert payload["addressed_to_you"][0]["said"] == \
            "bartimus explain yourself"

    def test_it_is_absent_when_nobody_named_us(self):
        payload = b.build_payload([_m("who's up")], {}, direct=[])
        assert "addressed_to_you" not in payload

    def test_the_brief_tells_it_to_answer_those_first(self):
        assert "addressed_to_you" in b.SYSTEM
        assert "waiting" in b.SYSTEM.lower()
