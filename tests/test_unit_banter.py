"""Unit tests for the draft-room banter agent (wes_banter, #039).

Network-free: read, send and the model call are all injected. What is tested is
RESTRAINT -- this is the one agent here whose output is free text going to real
people, so the failure that matters is volume, not quality.
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "pc"))

from sleeper import banter as banter  # noqa: E402


def _msgs(*pairs):
    return [{"author": a, "text": t, "system": not a} for a, t in pairs]


def _model(line):
    return lambda _b: json.dumps({"message": line})


class TestNewMessages:
    def test_our_own_messages_are_never_answered(self):
        """Two agents in a room become an infinite loop with an audience."""
        got = banter.new_messages(_msgs(("awarmwalrus", "hi")), set(),
                                  "awarmwalrus")
        assert got == []

    def test_the_match_ignores_case(self):
        got = banter.new_messages(_msgs(("AWarmWalrus", "hi")), set(),
                                  "awarmwalrus")
        assert got == []

    def test_system_messages_are_never_answered(self):
        """Answering "X has joined the draft!" is how a bot announces itself."""
        got = banter.new_messages([{"author": "", "text": "bob has joined!",
                                    "system": True}], set(), "me")
        assert got == []

    def test_messages_already_seen_are_skipped(self):
        got = banter.new_messages(_msgs(("bob", "old"), ("bob", "new")),
                                  {"old"}, "me")
        assert [m["text"] for m in got] == ["new"]

    def test_other_peoples_new_messages_come_through(self):
        got = banter.new_messages(_msgs(("bob", "nice pick")), set(), "me")
        assert len(got) == 1


class TestCompose:
    CHAT = _msgs(("bob", "who are you taking?"))

    def test_it_returns_the_line(self):
        assert banter.compose(self.CHAT, _post_fn=_model("Someone good.")) \
            == "Someone good."

    def test_an_empty_reply_means_stay_quiet(self):
        """Silence is a valid and frequent answer, not a failure."""
        assert banter.compose(self.CHAT, _post_fn=_model("")) is None

    def test_an_overlong_reply_is_dropped_not_truncated(self):
        """Truncating mid-sentence reads worse than saying nothing."""
        assert banter.compose(self.CHAT,
                              _post_fn=_model("x" * 400)) is None

    def test_a_dead_model_stays_quiet(self):
        assert banter.compose(self.CHAT, _post_fn=lambda _b: "not json") is None

    def test_no_messages_means_no_call(self):
        called = []
        banter.compose([], _post_fn=lambda _b: called.append(1) or "{}")
        assert called == []

    def test_it_sends_the_recent_chat_to_the_model(self):
        seen = {}

        def capture(body):
            seen.update(json.loads(body))
            return json.dumps({"message": "ok"})
        banter.compose(self.CHAT, context={"round": 3}, _post_fn=capture)
        sent = json.loads(seen["messages"][1]["content"])
        assert sent["recent_chat"][0]["said"] == "who are you taking?"
        assert sent["draft"]["round"] == 3


class TestBanterLoop:
    def _b(self, mode="auto", now=None, gap=180.0):
        return banter.Banter("D", me="me", mode=mode, min_gap_s=gap,
                             _now=now or (lambda: 1000.0))

    def test_the_first_pass_only_primes(self):
        """Everything already in the room is history. Without this the bot
        opens by answering a conversation that finished an hour ago."""
        b = self._b()
        sent = []
        act, detail = b.tick(_read_fn=lambda d: _msgs(("bob", "old chatter")),
                             _send_fn=lambda d, t: sent.append(t) or True,
                             _post_fn=_model("hello!"))
        assert act == "quiet" and "primed" in detail
        assert sent == []

    def test_it_answers_a_genuinely_new_message(self):
        b = self._b()
        b.tick(_read_fn=lambda d: _msgs(("bob", "old")), _post_fn=_model(""))
        sent = []
        act, detail = b.tick(
            _read_fn=lambda d: _msgs(("bob", "old"), ("bob", "new one")),
            _send_fn=lambda d, t: sent.append(t) or True,
            _post_fn=_model("nice."))
        assert act == "said" and sent == ["nice."]

    def test_the_rate_limit_is_enforced_in_CODE(self):
        """A small model asked to "be occasional" is not a rate limiter."""
        t = [1000.0]
        b = self._b(now=lambda: t[0])
        b.tick(_read_fn=lambda d: [], _post_fn=_model(""))
        b.tick(_read_fn=lambda d: _msgs(("bob", "one")),
               _send_fn=lambda d, x: True, _post_fn=_model("a"))
        t[0] += 5
        act, _d = b.tick(_read_fn=lambda d: _msgs(("bob", "two")),
                         _send_fn=lambda d, x: True, _post_fn=_model("b"))
        assert act == "rate_limited"

    def test_the_gap_eventually_expires(self):
        t = [1000.0]
        b = self._b(now=lambda: t[0], gap=60.0)
        b.tick(_read_fn=lambda d: [], _post_fn=_model(""))
        b.tick(_read_fn=lambda d: _msgs(("bob", "one")),
               _send_fn=lambda d, x: True, _post_fn=_model("a"))
        t[0] += 120
        act, _d = b.tick(_read_fn=lambda d: _msgs(("bob", "one"), ("bob", "two")),
                         _send_fn=lambda d, x: True, _post_fn=_model("b"))
        assert act == "said"

    def test_propose_mode_composes_but_posts_NOTHING(self):
        """The default. Posting to other people is not switched on by
        accident."""
        b = self._b(mode="propose")
        sent = []
        b.tick(_read_fn=lambda d: [], _post_fn=_model(""))
        act, detail = b.tick(_read_fn=lambda d: _msgs(("bob", "hi")),
                             _send_fn=lambda d, t: sent.append(t) or True,
                             _post_fn=_model("would say this"))
        # The detail now carries the exchange, not just our half of it.
        assert act == "would_say"
        assert detail.startswith("would say this")
        assert "bob: hi" in detail
        assert sent == []

    def test_propose_mode_is_rate_limited_too(self):
        """Otherwise the log fills as fast as the chat would have."""
        t = [1000.0]
        b = self._b(mode="propose", now=lambda: t[0])
        b.tick(_read_fn=lambda d: [], _post_fn=_model(""))
        b.tick(_read_fn=lambda d: _msgs(("bob", "one")), _post_fn=_model("a"))
        t[0] += 5
        act, _d = b.tick(_read_fn=lambda d: _msgs(("bob", "one"), ("bob", "two")),
                         _post_fn=_model("b"))
        assert act == "rate_limited"

    def test_off_reads_nothing_at_all(self):
        b = self._b(mode="off")
        reads = []
        act, _d = b.tick(_read_fn=lambda d: reads.append(1) or [])
        assert act == "quiet" and reads == []

    def test_a_failed_send_still_starts_the_clock(self):
        """A failing send retried every poll is exactly the flood this
        exists to prevent."""
        t = [1000.0]
        b = self._b(now=lambda: t[0])
        b.tick(_read_fn=lambda d: [], _post_fn=_model(""))
        act, _d = b.tick(_read_fn=lambda d: _msgs(("bob", "hi")),
                         _send_fn=lambda d, x: False, _post_fn=_model("a"))
        assert act == "send_failed"
        t[0] += 5
        act2, _d2 = b.tick(_read_fn=lambda d: _msgs(("bob", "hi"), ("bob", "yo")),
                           _send_fn=lambda d, x: False, _post_fn=_model("b"))
        assert act2 == "rate_limited"

    def test_a_broken_chat_read_never_raises_into_the_draft(self):
        def boom(_d):
            raise RuntimeError("chat panel gone")
        b = self._b()
        act, detail = b.tick(_read_fn=boom)
        assert act == "error" and "chat panel gone" in detail

    def test_a_broken_send_never_raises_into_the_draft(self):
        def boom(_d, _t):
            raise RuntimeError("no message box")
        b = self._b()
        b.tick(_read_fn=lambda d: [], _post_fn=_model(""))
        act, detail = b.tick(_read_fn=lambda d: _msgs(("bob", "hi")),
                             _send_fn=boom, _post_fn=_model("a"))
        assert act == "error" and "no message box" in detail


class TestExchangeLogging:
    """A transcript of only our own lines reads like a bot talking to itself
    -- which is also the failure worth spotting early."""

    def test_a_sent_line_records_what_prompted_it(self):
        b = banter.Banter("D", me="me", mode="auto", min_gap_s=0,
                          _now=lambda: 1000.0)
        b.tick(_read_fn=lambda d: [], _post_fn=_model(""))
        _act, detail = b.tick(
            _read_fn=lambda d: _msgs(("bob", "your team is bad")),
            _send_fn=lambda d, t: True, _post_fn=_model("bold words"))
        assert "bold words" in detail and "bob: your team is bad" in detail

    def test_staying_quiet_records_what_it_declined_to_answer(self):
        b = banter.Banter("D", me="me", mode="auto", min_gap_s=0,
                          _now=lambda: 1000.0)
        b.tick(_read_fn=lambda d: [], _post_fn=_model(""))
        act, detail = b.tick(_read_fn=lambda d: _msgs(("bob", "hello there")),
                             _post_fn=_model(""))
        assert act == "quiet" and "bob: hello there" in detail
