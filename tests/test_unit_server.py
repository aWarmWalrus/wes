"""Fast unit tests for pure server logic — no network, no API, no model loads.

Run: python -m pytest tests/        (from the wes-pc venv)
These are the regression tripwires for the server's non-LLM logic.

Nine classes were removed on 2026-09-02 when the Raspberry Pi tier retired and
the voice/vision half of the server went with it: TestVlmPrompt,
TestDescribeSceneCache, TestFaceSummary and TestSceneContext (camera + face
recognition), TestNextSentence and TestTtsClean (TTS sentence splitting and
markdown stripping), TestSttBias (Whisper contextual biasing), and TestNorm +
TestSpeculationLookup (the speculative-reply cache). They were deleted rather
than skipped — the code they covered no longer exists. See archive/pi/README.md.
"""
import json
import re
import time

import pytest

import wes_server as ws


class TestRateLimitBudget:
    """The Claude call-rate counter. It used to gate speculative calls as well;
    speculation went with the microphone, but the counter still runs."""

    def setup_method(self):
        with ws._rate_lock:
            ws._rate_calls.clear()

    def test_counts_recent_calls(self):
        for _ in range(3):
            ws._record_llm_call()
        assert ws._llm_calls_last_min() == 3

    def test_calls_older_than_a_minute_expire(self):
        with ws._rate_lock:
            ws._rate_calls.append(time.time() - 120)
        assert ws._llm_calls_last_min() == 0


class TestRunTool:
    def test_get_datetime_returns_a_year(self):
        out = ws.run_tool("get_datetime", {})
        assert any(y in out for y in ("2025", "2026", "2027", "2028"))

    def test_get_datetime_is_speakable(self):
        # gemma repeats this near-verbatim into TTS: month by name, 12h clock
        # with AM/PM, no seconds, no zero-padded hour/day
        out = ws.run_tool("get_datetime", {})
        assert re.search(r"January|February|March|April|May|June|July|August"
                         r"|September|October|November|December", out)
        assert re.search(r"\b\d{1,2}:\d{2} [AP]M", out)
        assert not re.search(r"\d{1,2}:\d{2}:\d{2}", out)  # no seconds
        assert " 0" not in out                             # no zero padding

    def test_unknown_tool(self):
        assert "unknown tool" in ws.run_tool("does_not_exist", {})

    def test_lookup_hosts_returns_registry(self):
        out = ws.run_tool("lookup_hosts", {})
        # From hosts.yaml. The Pi's 10.0.0.79 was asserted here too until that
        # host was retired (2026-09-02); a port is checked instead so the
        # assertion still fails if the summary stops carrying real detail.
        assert "DESKTOP-R2PFF9T.local" in out and "server 8080" in out

    def test_lookup_hosts_is_registered(self):
        names = [t["name"] for t in ws.TOOLS]
        assert "lookup_hosts" in names

    def test_fantasy_my_team_is_registered(self):
        names = [t["name"] for t in ws.TOOLS]
        assert "fantasy_my_team" in names

    def test_fantasy_my_team_dispatches_to_yahoo(self, monkeypatch):
        monkeypatch.setattr(ws.wes_yahoo, "fantasy_my_team",
                            lambda team=None: f"roster for {team!r}")
        assert ws.run_tool("fantasy_my_team", {"team": "Dinosaurs"}) \
            == "roster for 'Dinosaurs'"
        assert ws.run_tool("fantasy_my_team", {}) == "roster for None"

    def test_fantasy_player_value_is_registered(self):
        assert "fantasy_player_value" in [t["name"] for t in ws.TOOLS]

    def test_fantasy_player_value_dispatches(self, monkeypatch):
        monkeypatch.setattr(ws.wes_fantasy, "fantasy_player_value",
                            lambda player, versus=None: f"{player} vs {versus}")
        assert ws.run_tool("fantasy_player_value",
                           {"player": "Cam Thomas", "versus": "Luka"}) \
            == "Cam Thomas vs Luka"
        assert ws.run_tool("fantasy_player_value", {"player": "Cam Thomas"}) \
            == "Cam Thomas vs None"

    def test_fantasy_optimize_lineup_is_registered(self):
        """Registered 2026-07-29 after being held back all offseason — there is
        now a real drafted NFL roster to point it at."""
        assert "fantasy_optimize_lineup" in [t["name"] for t in ws.TOOLS]

    def test_fantasy_optimize_lineup_dispatches(self, monkeypatch):
        monkeypatch.setattr(ws.wes_fantasy, "fantasy_optimize_lineup",
                            lambda team=None: f"lineup for {team!r}")
        assert ws.run_tool("fantasy_optimize_lineup", {}) == "lineup for None"
        assert ws.run_tool("fantasy_optimize_lineup", {"team": "Pop"}) \
            == "lineup for 'Pop'"

    def test_optimize_lineup_tool_description_says_it_only_advises(self):
        """The tool never writes to Yahoo. If the description stops saying so, the
        model starts claiming it set the lineup — a false statement about a real
        account, which is worse than an unhelpful answer."""
        tool = next(t for t in ws.TOOLS
                    if t["name"] == "fantasy_optimize_lineup")
        desc = tool["description"].lower()
        assert "advise" in desc or "advises" in desc
        assert "never" in desc and "yahoo" in desc

    def test_fantasy_propose_lineup_change_is_registered(self):
        assert "fantasy_propose_lineup_change" in [t["name"] for t in ws.TOOLS]

    def test_fantasy_propose_lineup_change_dispatches(self, monkeypatch):
        monkeypatch.setattr(ws.wes_execute, "propose_lineup_change",
                            lambda team=None: f"proposed for {team!r}")
        assert ws.run_tool("fantasy_propose_lineup_change", {}) \
            == "proposed for None"
        assert ws.run_tool("fantasy_propose_lineup_change", {"team": "Pop"}) \
            == "proposed for 'Pop'"

    def test_propose_lineup_tool_description_never_lets_the_model_assume(self):
        """This tool CAN write to Yahoo now (2026-07-30), but whether a given
        call actually does depends on config the model can't see. The
        description must tell the model to relay whatever the tool's own reply
        says, never to assume either way — a false claim about a real account
        (in either direction) is the failure this guards."""
        tool = next(t for t in ws.TOOLS
                    if t["name"] == "fantasy_propose_lineup_change")
        desc = tool["description"].lower()
        assert "never assume" in desc
        assert "reply" in desc or "reports" in desc

    def test_nba_schedule_is_registered(self):
        assert "nba_schedule" in [t["name"] for t in ws.TOOLS]

    def test_nba_schedule_dispatches(self, monkeypatch):
        monkeypatch.setattr(ws.wes_nba, "next_game",
                            lambda team=None: f"next game for {team!r}")
        assert ws.run_tool("nba_schedule", {"team": "Celtics"}) \
            == "next game for 'Celtics'"
        assert ws.run_tool("nba_schedule", {}) == "next game for None"

    def test_nba_top_performers_is_registered(self):
        assert "nba_top_performers" in [t["name"] for t in ws.TOOLS]

    def test_nba_top_performers_dispatches(self, monkeypatch):
        monkeypatch.setattr(ws.wes_nba, "top_performers",
                            lambda team=None: f"leaders for {team!r}")
        assert ws.run_tool("nba_top_performers", {"team": "Nets"}) \
            == "leaders for 'Nets'"
        assert ws.run_tool("nba_top_performers", {}) == "leaders for None"


class TestConversationMemory:
    """Sliding-window conversation memory (LiveKit ChatContext pattern)."""

    def setup_method(self):
        ws.reset_conversation()

    def teardown_method(self):
        ws.reset_conversation()

    def test_round_trip_in_order(self):
        ws.record_turn("hi", "hello there")
        ws.record_turn("what time is it", "It is noon.")
        assert ws.conversation_context() == [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello there"},
            {"role": "user", "content": "what time is it"},
            {"role": "assistant", "content": "It is noon."},
        ]

    def test_silence_and_empty_replies_are_not_memory(self):
        ws.record_turn("", "Sorry, I didn't catch that.")
        ws.record_turn("   ", "reply")
        ws.record_turn("question", "")
        assert ws.conversation_context() == []

    def test_window_keeps_last_n_exchanges(self):
        for i in range(ws.CONV_TURNS + 3):
            ws.record_turn(f"q{i}", f"a{i}")
        ctx = ws.conversation_context()
        assert len(ctx) == 2 * ws.CONV_TURNS
        assert ctx[0]["content"] == "q3"      # oldest three dropped
        assert ctx[-1]["content"] == f"a{ws.CONV_TURNS + 2}"

    def test_idle_ttl_clears_context(self):
        ws.record_turn("hi", "hello")
        ws._conv_last["text"] = time.time() - ws.CONV_TTL - 1
        assert ws.conversation_context() == []

    def test_channels_are_isolated(self):
        ws.record_turn("default question", "default answer")
        ws.record_turn("remote question", "remote answer", channel="discord")
        assert [m["content"] for m in ws.conversation_context()] == \
            ["default question", "default answer"]
        assert [m["content"] for m in ws.conversation_context("discord")] == \
            ["remote question", "remote answer"]

    def test_reset_one_channel_leaves_others(self):
        ws.record_turn("a", "b")
        ws.record_turn("c", "d", channel="discord")
        assert ws.reset_conversation("discord") == 1
        assert ws.conversation_context("discord") == []
        assert len(ws.conversation_context()) == 2

    def test_reset_default_clears_all_channels(self):
        ws.record_turn("a", "b")
        ws.record_turn("c", "d", channel="discord")
        assert ws.reset_conversation() == 2
        assert ws.conversation_context() == []
        assert ws.conversation_context("discord") == []

    def test_reset_returns_turn_count(self):
        ws.record_turn("a", "b")
        ws.record_turn("c", "d")
        assert ws.reset_conversation() == 2
        assert ws.conversation_context() == []

    # Three barge-in tests lived here (record_spoken_turn tagging a reply the
    # user cut off mid-playback). Nothing can be interrupted mid-delivery now
    # that every channel is text — see archive/pi/README.md.

    def test_roles_always_alternate_user_first(self):
        # the Anthropic API rejects non-alternating roles; the window cap is
        # in whole exchanges so this must hold for any history state
        for i in range(ws.CONV_TURNS + 5):
            ws.record_turn(f"q{i}", f"a{i}")
        ctx = ws.conversation_context()
        assert [m["role"] for m in ctx[0::2]] == ["user"] * (len(ctx) // 2)
        assert [m["role"] for m in ctx[1::2]] == ["assistant"] * (len(ctx) // 2)

    # --- Phase 0 (#023): per-channel depth + persistence -------------------

    def test_discord_window_is_deeper_than_the_default(self):
        """Discord gets an explicit deep, long-lived window; anything without a
        policy falls back to the shallow default. That default was tuned for the
        retired voice channel and is deliberately still small — a channel nobody
        configured should get the cheap window, not Discord's 40-turn one."""
        dmax = ws._conv_policy("discord")[0]
        default_max = ws._conv_policy("something_new")[0]
        assert dmax > default_max
        for i in range(default_max + 10):
            ws.record_turn(f"dq{i}", f"da{i}", channel="discord")
            ws.record_turn(f"oq{i}", f"oa{i}", channel="other")
        assert len(ws.conversation_context("discord")) == 2 * (default_max + 10)
        assert len(ws.conversation_context("other")) == 2 * default_max

    def test_discord_ttl_outlives_the_default_ttl(self):
        # a gap that would expire the default channel must NOT expire discord
        ws.record_turn("q", "a", channel="discord")
        ws._conv_last["discord"] = time.time() - ws.CONV_TTL - 1
        assert ws.conversation_context("discord") != []

    def test_no_bleed_discord_content_absent_from_other_channels(self):
        ws.record_turn("secret discord thing", "ok", channel="discord")
        joined = " ".join(m["content"] for m in ws.conversation_context("other"))
        assert "secret discord thing" not in joined
        assert ws.conversation_context("other") == []

    def test_window_survives_restart(self):
        ws.record_turn("my name is charlie", "Hi Charlie.", channel="discord")
        ws.record_turn("i like purple", "Noted.", channel="discord")
        # simulate a server restart: drop RAM state, reload from disk
        ws._convs.clear()
        ws._conv_last.clear()
        ws.load_conversations()
        ctx = ws.conversation_context("discord")
        assert [m["content"] for m in ctx] == \
            ["my name is charlie", "Hi Charlie.", "i like purple", "Noted."]

    def test_stale_window_not_reloaded(self, monkeypatch):
        import os
        ws.record_turn("old", "reply", channel="other")
        path = ws._conv_file("other")
        # age the file past the default TTL
        old = time.time() - ws.CONV_TTL - 10
        os.utime(path, (old, old))
        ws._convs.clear()
        ws._conv_last.clear()
        ws.load_conversations()
        assert ws.conversation_context("other") == []

    def test_reset_removes_persisted_file(self):
        import os
        ws.record_turn("a", "b", channel="discord")
        assert os.path.exists(ws._conv_file("discord"))
        ws.reset_conversation("discord")
        assert not os.path.exists(ws._conv_file("discord"))


class TestOllamaBackend:
    """Local (Ollama) LLM backend — schema conversion, tool loop, fallback.
    All network-free: urlopen is monkeypatched."""

    def test_tool_schema_conversion(self):
        out = ws._ollama_tools()
        assert len(out) == len(ws.TOOLS)
        names = [t["function"]["name"] for t in out]
        assert "fantasy_optimize_lineup" in names and "get_datetime" in names
        for t in out:
            assert t["type"] == "function"
            assert "type" in t["function"]["parameters"]

    @staticmethod
    def _fake_urlopen(responses):
        """Fake urlopen serving canned /api/chat streams. Returns (fn, calls):
        calls[i] is the parsed request body of the i-th call."""
        calls = []

        class FakeResp:
            def __init__(self, chunks):
                self._chunks = chunks

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def __iter__(self):
                return (json.dumps(c).encode() + b"\n" for c in self._chunks)

        seq = iter(responses)

        def fake(req, timeout=None):
            calls.append(json.loads(req.data))
            return FakeResp(next(seq))

        return fake, calls

    def test_stream_local_plain_reply(self, monkeypatch):
        fake, calls = self._fake_urlopen([[
            {"message": {"content": "Hello"}, "done": False},
            {"message": {"content": " there."}, "done": True},
        ]])
        monkeypatch.setattr(ws.urllib.request, "urlopen", fake)
        assert "".join(ws._stream_local("hi")) == "Hello there."
        assert calls[0]["model"] == ws.LOCAL_LLM_MODEL
        assert calls[0]["think"] is False  # spoken replies: no thinking tokens

    def test_stream_local_carries_conversation(self, monkeypatch):
        ws.reset_conversation()
        ws.record_turn("my name is charlie", "Nice to meet you, Charlie.")
        fake, calls = self._fake_urlopen([[
            {"message": {"content": "You are Charlie."}, "done": True}]])
        monkeypatch.setattr(ws.urllib.request, "urlopen", fake)
        try:
            out = "".join(ws._stream_local("what is my name?"))
        finally:
            ws.reset_conversation()
        assert out == "You are Charlie."
        msgs = calls[0]["messages"]
        assert msgs[0]["role"] == "system"
        assert msgs[1:] == [
            {"role": "user", "content": "my name is charlie"},
            {"role": "assistant", "content": "Nice to meet you, Charlie."},
            {"role": "user", "content": "what is my name?"},
        ]

    def test_stream_local_tool_loop(self, monkeypatch):
        fake, calls = self._fake_urlopen([
            [{"message": {"content": "", "tool_calls": [
                {"function": {"name": "get_datetime", "arguments": {}}}]},
              "done": True}],
            [{"message": {"content": "It is noon."}, "done": True}],
        ])
        monkeypatch.setattr(ws.urllib.request, "urlopen", fake)
        monkeypatch.setattr(ws, "run_tool", lambda name, args: "noon")
        assert "".join(ws._stream_local("time?")) == "It is noon."
        # The second round must carry the tool result back to the model.
        roles = [m["role"] for m in calls[1]["messages"]]
        assert "tool" in roles

    def test_deep_tier_empty_falls_back_to_claude(self, monkeypatch):
        # a hard problem can make the 12b+thinking tier spend its whole budget on
        # thinking and emit NO content -> empty reply ("(no reply)" on Discord).
        # It must fall back to Claude rather than return "". (Jacobian repro.)
        fake, _ = self._fake_urlopen([[{"message": {}, "done": True}]])
        monkeypatch.setattr(ws.urllib.request, "urlopen", fake)
        monkeypatch.setattr(ws, "_stream_claude",
                            lambda t, channel="voice": iter(["Verified for you."]))
        assert "".join(ws._stream_local("hard", deep=True)) == "Verified for you."

    def test_fast_tier_empty_does_not_fall_back(self, monkeypatch):
        # the fallback is scoped to the deep (thinking) tier; a fast-router empty
        # reply is a different, rare case and must stay unchanged (no Claude cost)
        fake, _ = self._fake_urlopen([[{"message": {}, "done": True}]])
        monkeypatch.setattr(ws.urllib.request, "urlopen", fake)
        monkeypatch.setattr(
            ws, "_stream_claude",
            lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not fall back")))
        assert "".join(ws._stream_local("x", deep=False)) == ""

    def test_deep_default_effort_uses_full_budget(self, monkeypatch):
        # deep tier with no explicit effort (the Discord-router path) keeps the
        # full "deep" budget so #026 never changes deep-by-default behavior.
        fake, calls = self._fake_urlopen([[{"message": {"content": "ok"},
                                            "done": True}]])
        monkeypatch.setattr(ws.urllib.request, "urlopen", fake)
        "".join(ws._stream_local("hard", deep=True))
        assert calls[0]["think"] is True
        assert calls[0]["options"]["num_predict"] == ws.EFFORT_BUDGET["deep"][1]

    def test_deep_standard_effort_uses_modest_budget(self, monkeypatch):
        # an escalation the router marked "standard" gets the cheaper rung, not
        # the full deep allowance.
        fake, calls = self._fake_urlopen([[{"message": {"content": "ok"},
                                            "done": True}]])
        monkeypatch.setattr(ws.urllib.request, "urlopen", fake)
        "".join(ws._stream_local("q", deep=True, effort="standard"))
        assert calls[0]["think"] is True
        assert calls[0]["options"]["num_predict"] == ws.EFFORT_BUDGET["standard"][1]
        assert ws.EFFORT_BUDGET["standard"][1] < ws.EFFORT_BUDGET["deep"][1]

    def test_unknown_effort_falls_back_to_default(self, monkeypatch):
        fake, calls = self._fake_urlopen([[{"message": {"content": "ok"},
                                            "done": True}]])
        monkeypatch.setattr(ws.urllib.request, "urlopen", fake)
        "".join(ws._stream_local("q", deep=True, effort="bogus"))
        want = ws.EFFORT_BUDGET[ws.DEFAULT_EFFORT][1]
        assert calls[0]["options"]["num_predict"] == want

    def test_fast_tier_ignores_effort(self, monkeypatch):
        # the fast router is a fixed thinking-off / 512 regardless of effort.
        fake, calls = self._fake_urlopen([[{"message": {"content": "ok"},
                                            "done": True}]])
        monkeypatch.setattr(ws.urllib.request, "urlopen", fake)
        "".join(ws._stream_local("q", deep=False, effort="deep"))
        assert calls[0]["think"] is False
        assert calls[0]["options"]["num_predict"] == 512

    def test_router_effort_flows_to_deep_tier(self, monkeypatch):
        # the router escalates WITH an effort arg; that effort must size the
        # deep tier's budget (end-to-end: fast round -> escalate -> deep round).
        monkeypatch.setattr(ws, "ESCALATE_MODEL", "gemma4:12b")
        monkeypatch.setattr(ws, "ESCALATE_ACK", "")  # no ack text to strip
        fake, calls = self._fake_urlopen([
            [{"message": {"content": "", "tool_calls": [{"function": {
                "name": "escalate_hard",
                "arguments": {"reason": "hard", "effort": "deep"}}}]},
              "done": True}],
            [{"message": {"content": "Deep answer."}, "done": True}],
        ])
        monkeypatch.setattr(ws.urllib.request, "urlopen", fake)
        out = "".join(ws._stream_local("prove it", deep=False, buffered=True))
        assert "Deep answer." in out
        # calls[1] is the deep round; it must carry the "deep" budget + thinking.
        assert calls[1]["think"] is True
        assert calls[1]["options"]["num_predict"] == ws.EFFORT_BUDGET["deep"][1]

    def test_router_standard_effort_flows_to_deep_tier(self, monkeypatch):
        monkeypatch.setattr(ws, "ESCALATE_MODEL", "gemma4:12b")
        monkeypatch.setattr(ws, "ESCALATE_ACK", "")
        fake, calls = self._fake_urlopen([
            [{"message": {"content": "", "tool_calls": [{"function": {
                "name": "escalate_hard",
                "arguments": {"reason": "meh", "effort": "standard"}}}]},
              "done": True}],
            [{"message": {"content": "Standard answer."}, "done": True}],
        ])
        monkeypatch.setattr(ws.urllib.request, "urlopen", fake)
        out = "".join(ws._stream_local("q", deep=False, buffered=True))
        assert "Standard answer." in out
        assert calls[1]["options"]["num_predict"] == ws.EFFORT_BUDGET["standard"][1]

    def test_local_failure_falls_back_to_claude(self, monkeypatch):
        """Ollama being down must not lose the turn.

        This used to assert on `stream_reply`, the voice path's generator, which
        also had to decide whether a MID-reply failure could be restarted on
        another backend — it could not, because the earlier half had already
        been spoken, so it apologised in place instead. Every turn is buffered
        now (nothing reaches the user until it is complete), so `think` can
        simply fall back whole and that special case is gone."""
        monkeypatch.setattr(ws, "LLM_BACKEND", "local")

        def boom(transcript, channel="text", use_tools=True):
            raise OSError("ollama down")

        monkeypatch.setattr(ws, "_think_local", boom)
        monkeypatch.setattr(ws, "_think_claude",
                            lambda t, channel="text": "claude reply")
        assert ws.think("hi") == "claude reply"

    def test_partial_local_reply_still_falls_back_whole(self, monkeypatch):
        """A failure after some deltas were generated is still a clean fallback:
        the caller joined them and delivered nothing, so there is no half-spoken
        reply to preserve and Claude answers the whole question."""
        monkeypatch.setattr(ws, "LLM_BACKEND", "local")

        def partial(transcript, channel="text", use_tools=True):
            raise OSError("connection dropped")

        monkeypatch.setattr(ws, "_think_local", partial)
        monkeypatch.setattr(ws, "_think_claude",
                            lambda t, channel="text": "complete claude answer")
        assert ws.think("hi") == "complete claude answer"


class TestEscalation:
    """Smart routing: the local model calls escalate_hard to hand off."""

    def test_toolset_includes_escalation_when_claude_available(self, monkeypatch):
        monkeypatch.setattr(ws, "ESCALATE", True)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        names = [t["function"]["name"] for t in ws._local_toolset()]
        assert "escalate_hard" in names
        # ...but never in the shared TOOLS Claude itself sees.
        assert all(t["name"] != "escalate_hard" for t in ws.TOOLS)

    def test_toolset_omits_escalation_without_key(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        names = [t["function"]["name"] for t in ws._local_toolset()]
        assert "escalate_hard" not in names

    def test_toolset_omits_escalation_when_disabled(self, monkeypatch):
        monkeypatch.setattr(ws, "ESCALATE", False)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        names = [t["function"]["name"] for t in ws._local_toolset()]
        assert "escalate_hard" not in names

    def test_escalate_tool_exposes_effort_knob(self):
        # #026: the router can size the thinking budget when it hands off.
        props = ws.ESCALATE_TOOL["function"]["parameters"]["properties"]
        assert "effort" in props
        assert set(props["effort"]["enum"]) == {"standard", "deep"}
        # every offered effort must be a real budget entry
        assert set(props["effort"]["enum"]) <= set(ws.EFFORT_BUDGET)


class TestWebSearch:
    """search_web: the router hands a LIVE-INFO query to Haiku + web search.
    Distinct from escalate_hard (hard reasoning -> local 12b+thinking)."""

    def test_toolset_offers_search_web_when_available(self, monkeypatch):
        monkeypatch.setattr(ws, "WEB_SEARCH", True)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        for deep in (False, True):
            names = [t["function"]["name"] for t in ws._local_toolset(deep=deep)]
            assert "search_web" in names, f"deep={deep}"
        # the server-side web_search tool is NEVER exposed to the local router
        assert all(t["function"]["name"] != "web_search"
                   for t in ws._local_toolset())

    def test_search_web_omitted_without_key(self, monkeypatch):
        monkeypatch.setattr(ws, "WEB_SEARCH", True)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        names = [t["function"]["name"] for t in ws._local_toolset()]
        assert "search_web" not in names

    def test_search_web_omitted_when_disabled(self, monkeypatch):
        monkeypatch.setattr(ws, "WEB_SEARCH", False)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        names = [t["function"]["name"] for t in ws._local_toolset()]
        assert "search_web" not in names

    def test_deep_tier_gets_search_web_but_not_escalate(self, monkeypatch):
        # The deep tier can't reach the web itself (needs Haiku), but it must NOT
        # carry escalate_hard — it already IS the reasoning escalation.
        monkeypatch.setattr(ws, "WEB_SEARCH", True)
        monkeypatch.setattr(ws, "ESCALATE", True)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        names = [t["function"]["name"] for t in ws._local_toolset(deep=True)]
        assert "search_web" in names
        assert "escalate_hard" not in names

    def test_use_tools_false_sends_no_tools_at_all(self, monkeypatch):
        """Phrasing-only work (announce for a fantasy write report) must carry
        NO schemas — a rewrite-this-sentence task that calls a tool has
        misunderstood its job. Also the single largest per-call context saving
        available (~2.5k tokens), but correctness is the reason."""
        monkeypatch.setattr(ws, "WEB_SEARCH", True)
        monkeypatch.setattr(ws, "ESCALATE", True)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        assert ws._local_toolset(use_tools=False) == []
        # ...and the handoff tools must not sneak back in either.
        assert ws._local_toolset(deep=True, use_tools=False) == []

    def test_use_tools_defaults_true_so_existing_callers_are_unchanged(self):
        assert ws._local_toolset() != []

    def test_router_hands_search_web_to_claude_with_web(self, monkeypatch):
        monkeypatch.setattr(ws, "WEB_SEARCH", True)
        monkeypatch.setattr(ws, "WEB_SEARCH_ACK", "")  # no ack prefix in the assert
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        fake, _ = TestOllamaBackend._fake_urlopen([
            [{"message": {"content": "", "tool_calls": [
                {"function": {"name": "search_web",
                              "arguments": {"query": "weather London"}}}]},
              "done": True}],
        ])
        monkeypatch.setattr(ws.urllib.request, "urlopen", fake)
        seen = {}

        def fake_claude(transcript, channel="voice", web=False):
            seen["web"] = web
            seen["transcript"] = transcript
            return iter(["Sunny in London."])

        monkeypatch.setattr(ws, "_stream_claude", fake_claude)
        out = "".join(ws._stream_local("what's the weather in London"))
        assert out == "Sunny in London."
        assert seen["web"] is True  # handed off WITH web search on
        assert seen["transcript"] == "what's the weather in London"

    def test_stream_claude_web_adds_server_side_tool(self, monkeypatch):
        monkeypatch.setattr(ws, "WEB_SEARCH", True)
        captured = {}

        class _Usage:
            input_tokens = 1
            output_tokens = 1

        class _Final:
            stop_reason = "end_turn"
            content = []
            usage = _Usage()

        class _Stream:
            text_stream = ["Sunny."]

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def get_final_message(self):
                return _Final()

        class _Msgs:
            def stream(self, **kwargs):
                captured.update(kwargs)
                return _Stream()

        class _Client:
            messages = _Msgs()

        monkeypatch.setattr(ws, "get_anthropic", lambda: _Client())
        out = "".join(ws._stream_claude("weather?", web=True))
        assert out == "Sunny."
        types = [t.get("type") for t in captured["tools"]]
        assert "web_search_20250305" in types  # server-side web search attached
        assert captured["max_tokens"] == 1024

    def test_stream_claude_no_web_omits_server_tool(self, monkeypatch):
        captured = {}

        class _Final:
            stop_reason = "end_turn"
            content = []

            class usage:
                input_tokens = 1
                output_tokens = 1

        class _Stream:
            text_stream = ["Hi."]

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def get_final_message(self):
                return _Final()

        class _Client:
            class messages:
                @staticmethod
                def stream(**kwargs):
                    captured.update(kwargs)
                    return _Stream()

        monkeypatch.setattr(ws, "get_anthropic", lambda: _Client())
        "".join(ws._stream_claude("hi"))  # web defaults False
        types = [t.get("type") for t in captured["tools"]]
        assert "web_search_20250305" not in types


class TestUnkeptPromiseGuard:
    """#002: the router SAYS it will look into something but calls nothing, so
    the promise is never kept. Buffered channels can silently re-run deep."""

    def _notes(self, tools=(), escalated=False):
        ws._turn_begin()
        ws._turn_notes.tools = list(tools)
        ws._turn_notes.escalated = escalated

    def test_promise_with_no_tool_is_unkept(self, monkeypatch):
        monkeypatch.setattr(ws, "ESCALATE_MODEL", "gemma4:12b")
        self._notes()
        assert ws._is_unkept_promise("Sure — I'll look into it and get back to you.")

    def test_promise_followed_by_a_real_tool_is_kept(self, monkeypatch):
        # The regression this guard must not cause: "I'll check X" + a real tool
        # call is a KEPT promise and must be left completely alone.
        monkeypatch.setattr(ws, "ESCALATE_MODEL", "gemma4:12b")
        self._notes(tools=["get_system_status"])
        assert not ws._is_unkept_promise(
            "I'll check the system status for you. Nope, it's relaxed right now.")

    def test_already_escalated_is_not_unkept(self, monkeypatch):
        monkeypatch.setattr(ws, "ESCALATE_MODEL", "gemma4:12b")
        self._notes(escalated=True)
        assert not ws._is_unkept_promise("I'll look into it.")

    def test_plain_answer_is_not_a_promise(self, monkeypatch):
        monkeypatch.setattr(ws, "ESCALATE_MODEL", "gemma4:12b")
        self._notes()
        assert not ws._is_unkept_promise("The capital of France is Paris.")

    def test_no_escalation_target_leaves_the_promise_alone(self, monkeypatch):
        # Nowhere to retry -> a weak promise still beats no answer at all.
        monkeypatch.setattr(ws, "ESCALATE_MODEL", "")
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        self._notes()
        assert not ws._is_unkept_promise("I'll get back to you.")

    def test_think_local_replaces_unkept_promise(self, monkeypatch):
        monkeypatch.setattr(ws, "ESCALATE_MODEL", "gemma4:12b")
        monkeypatch.setattr(
            ws, "_stream_local",
            lambda *a, **k: iter(["I'll look into it and get back to you."]))
        seen = {}

        def fake_esc(transcript, channel="voice"):
            seen["transcript"] = transcript
            return iter(["Imagine a see-saw that always balances..."])

        monkeypatch.setattr(ws, "_stream_escalation", fake_esc)
        ws._turn_begin()
        out = ws._think_local("explain the quadratic formula to a toddler")
        assert out == "Imagine a see-saw that always balances..."
        assert ws.NO_DEFER_FRAMING in seen["transcript"]
        assert ws._turn_notes.escalated is True  # logged as an escalation

    def test_think_local_keeps_original_when_retry_is_empty(self, monkeypatch):
        # Never trade a weak answer for dead air.
        monkeypatch.setattr(ws, "ESCALATE_MODEL", "gemma4:12b")
        monkeypatch.setattr(ws, "_stream_local",
                            lambda *a, **k: iter(["I'll look into it."]))
        monkeypatch.setattr(ws, "_stream_escalation", lambda *a, **k: iter([""]))
        ws._turn_begin()
        assert ws._think_local("hard thing") == "I'll look into it."

    def test_think_local_leaves_a_good_reply_untouched(self, monkeypatch):
        monkeypatch.setattr(ws, "ESCALATE_MODEL", "gemma4:12b")
        monkeypatch.setattr(ws, "_stream_local",
                            lambda *a, **k: iter(["Paris."]))

        def boom(*a, **k):
            raise AssertionError("must not escalate a perfectly good reply")

        monkeypatch.setattr(ws, "_stream_escalation", boom)
        ws._turn_begin()
        assert ws._think_local("capital of france?") == "Paris."

    def test_escalation_hands_off_to_claude(self, monkeypatch):
        fake, calls = TestOllamaBackend._fake_urlopen([
            [{"message": {"content": "", "tool_calls": [
                {"function": {"name": "escalate_hard",
                              "arguments": {"reason": "hard math"}}}]},
              "done": True}],
        ])
        monkeypatch.setattr(ws, "ESCALATE_MODEL", "")  # Claude target
        monkeypatch.setattr(ws.urllib.request, "urlopen", fake)
        monkeypatch.setattr(
            ws, "_stream_claude", lambda t, channel="voice": iter(["Claude answer."]))
        out = list(ws._stream_local("prove this theorem"))
        # Server-injected ack first (masks Claude spin-up), then Claude's reply.
        assert out == [ws.ESCALATE_ACK, "Claude answer."]
        assert len(calls) == 1  # handed off — no further local rounds

    def test_escalation_ack_is_a_complete_sentence(self):
        # Asserted via next_sentence() until the TTS splitter retired with the
        # voice tier: the ack had to end terminator+space or the splitter would
        # hold it until the deep tier's first token, defeating its purpose. The
        # shape is still what the constant documents, so it is still checked.
        assert ws.ESCALATE_ACK.rstrip()[-1] in ".!?"
        assert ws.ESCALATE_ACK.endswith(" ")

    def test_escalation_ack_disabled_by_empty_string(self, monkeypatch):
        fake, _ = TestOllamaBackend._fake_urlopen([
            [{"message": {"content": "", "tool_calls": [
                {"function": {"name": "escalate_hard", "arguments": {}}}]},
              "done": True}],
        ])
        monkeypatch.setattr(ws.urllib.request, "urlopen", fake)
        monkeypatch.setattr(ws, "ESCALATE_ACK", "")
        monkeypatch.setattr(
            ws, "_stream_claude", lambda t, channel="voice": iter(["Claude answer."]))
        assert list(ws._stream_local("q")) == ["Claude answer."]

    def test_toolset_includes_escalation_with_local_target_no_key(self, monkeypatch):
        # A local deep model is a valid escalation target without any API key.
        monkeypatch.setattr(ws, "ESCALATE", True)
        monkeypatch.setattr(ws, "ESCALATE_MODEL", "gemma4:12b")
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        names = [t["function"]["name"] for t in ws._local_toolset()]
        assert "escalate_hard" in names

    def test_escalation_hands_off_to_local_deep_model(self, monkeypatch):
        fake, calls = TestOllamaBackend._fake_urlopen([
            [{"message": {"content": "", "tool_calls": [
                {"function": {"name": "escalate_hard",
                              "arguments": {"reason": "hard math"}}}]},
              "done": True}],
            [{"message": {"thinking": "let me reason...", "content": ""},
              "done": False},
             {"message": {"content": "Deep answer."}, "done": True}],
        ])
        monkeypatch.setattr(ws, "ESCALATE_MODEL", "gemma4:12b")
        monkeypatch.setattr(ws.urllib.request, "urlopen", fake)
        monkeypatch.setattr(
            ws, "_stream_claude",
            lambda t, channel="voice": (_ for _ in ()).throw(
                AssertionError("must stay local")))
        out = list(ws._stream_local("prove this theorem"))
        # Ack, then the deep reply — and the thinking delta is never yielded.
        assert out == [ws.ESCALATE_ACK, "Deep answer."]
        deep = calls[1]
        assert deep["model"] == "gemma4:12b"
        assert deep["think"] is True
        assert deep["options"]["num_predict"] > 512  # room for thinking
        # The deep tier must not see the escalate tool (no recursion).
        names = [t["function"]["name"] for t in deep.get("tools") or []]
        assert "escalate_hard" not in names and "get_datetime" in names
        # Router call is unchanged: default model, no thinking.
        assert calls[0]["model"] == ws.LOCAL_LLM_MODEL
        assert calls[0]["think"] is False

    def test_deep_tier_carries_conversation_and_channel(self, monkeypatch):
        ws.reset_conversation()
        ws.record_turn("hi", "hello", channel="discord")
        fake, calls = TestOllamaBackend._fake_urlopen([
            [{"message": {"content": "Deep answer."}, "done": True}]])
        monkeypatch.setattr(ws, "ESCALATE_MODEL", "gemma4:12b")
        monkeypatch.setattr(ws.urllib.request, "urlopen", fake)
        try:
            out = "".join(ws._stream_local("q", channel="discord", deep=True))
        finally:
            ws.reset_conversation()
        assert out == "Deep answer."
        msgs = calls[0]["messages"]
        assert {"role": "user", "content": "hi"} in msgs
        assert ws.TEXT_CHANNEL_NOTE in msgs[0]["content"]

    def test_buffered_retracts_spoken_announcement(self, monkeypatch):
        # gemma sometimes ANNOUNCES the handoff ("ask Claude...") instead of
        # silently escalating. In buffered mode (think(): /respond,
        # /respond_text, speculation) nothing reached the user yet, so the
        # announcement is retracted and replaced by the deep tier's answer.
        fake, _ = TestOllamaBackend._fake_urlopen([
            [{"message": {"content": "You should ask Claude about that. "},
              "done": False},
             {"message": {"content": "", "tool_calls": [
                 {"function": {"name": "escalate_hard", "arguments": {}}}]},
              "done": True}],
        ])
        monkeypatch.setattr(ws, "ESCALATE_MODEL", "")  # Claude target
        monkeypatch.setattr(ws.urllib.request, "urlopen", fake)
        monkeypatch.setattr(
            ws, "_stream_claude", lambda t, channel="voice": iter(["Deep answer."]))
        out = ws._think_local("prove this theorem")
        assert out == "Deep answer."  # no announcement, no ack

    def test_buffered_escalation_has_no_ack(self, monkeypatch):
        # The ack exists to mask dead air on the live voice stream; a buffered
        # reply arrives whole, so it must not be prepended there.
        fake, _ = TestOllamaBackend._fake_urlopen([
            [{"message": {"content": "", "tool_calls": [
                {"function": {"name": "escalate_hard", "arguments": {}}}]},
              "done": True}],
        ])
        monkeypatch.setattr(ws, "ESCALATE_MODEL", "")
        monkeypatch.setattr(ws.urllib.request, "urlopen", fake)
        monkeypatch.setattr(
            ws, "_stream_claude", lambda t, channel="voice": iter(["Deep answer."]))
        assert ws._think_local("q") == "Deep answer."

    def test_no_handoff_after_speech_started(self, monkeypatch):
        fake, calls = TestOllamaBackend._fake_urlopen([
            [{"message": {"content": "Well, "}, "done": False},
             {"message": {"content": "", "tool_calls": [
                 {"function": {"name": "escalate_hard", "arguments": {}}}]},
              "done": True}],
            [{"message": {"content": "here is my answer."}, "done": True}],
        ])
        monkeypatch.setattr(ws.urllib.request, "urlopen", fake)
        monkeypatch.setattr(
            ws, "_stream_claude",
            lambda t, channel="voice": (_ for _ in ()).throw(
                AssertionError("must not hand off")))
        assert "".join(ws._stream_local("q")) == "Well, here is my answer."
        # The suppressed escalation went back as a tool result.
        roles = [m["role"] for m in calls[1]["messages"]]
        assert "tool" in roles


class TestChannelSystemPrompt:
    """The persona is channel-agnostic; the presentation note is appended.

    There were two notes and `system_prompt` chose between them by channel. The
    voice one retired with the Pi (2026-09-02), so every channel is typed."""

    def test_every_channel_gets_the_text_note(self):
        for ch in ("discord", "text", "voice", "something_unknown"):
            p = ws.system_prompt(ch)
            assert ws.TEXT_CHANNEL_NOTE in p
            assert p.startswith(ws.SYSTEM_PROMPT)  # persona first

    def test_prompt_never_claims_a_speaker_or_camera(self):
        """The model must not offer capabilities the hardware no longer has —
        it will call a tool that isn't there and then narrate the failure."""
        p = ws.system_prompt("discord").lower()
        for gone in ("speaking this reply aloud", "text-to-speech",
                     "house's camera", "raspberry pi"):
            assert gone not in p

    def test_stream_local_uses_channel_prompt(self, monkeypatch):
        fake, calls = TestOllamaBackend._fake_urlopen([[
            {"message": {"content": "ok"}, "done": True}]])
        monkeypatch.setattr(ws.urllib.request, "urlopen", fake)
        "".join(ws._stream_local("hi", channel="discord"))
        assert ws.TEXT_CHANNEL_NOTE in calls[0]["messages"][0]["content"]


class TestRespondText:
    """Text-in/text-out endpoint for remote frontends (the Discord bot)."""

    def setup_method(self):
        ws.reset_conversation()

    def teardown_method(self):
        ws.reset_conversation()

    def test_reply_and_channel_memory(self, monkeypatch):
        monkeypatch.setattr(
            ws, "think", lambda text, channel="voice": f"[{channel}] {text}")
        with ws.app.test_client() as c:
            r = c.post("/respond_text",
                       json={"text": "hello", "channel": "discord"})
        assert r.status_code == 200
        assert r.get_json()["reply"] == "[discord] hello"
        # Recorded in the discord channel, not the voice one.
        assert ws.conversation_context() == []
        assert ws.conversation_context("discord")[-1]["content"] == \
            "[discord] hello"

    def test_default_channel_is_text(self, monkeypatch):
        monkeypatch.setattr(
            ws, "think", lambda text, channel="voice": "ok")
        with ws.app.test_client() as c:
            assert c.post("/respond_text", json={"text": "hi"}).status_code == 200
        assert ws.conversation_context("text") != []

    def test_empty_text_is_400(self):
        with ws.app.test_client() as c:
            assert c.post("/respond_text", json={"text": "  "}).status_code == 400
            assert c.post("/respond_text", data=b"").status_code == 400

    def test_reset_route_scopes_to_channel(self):
        ws.record_turn("a", "b")
        ws.record_turn("c", "d", channel="discord")
        with ws.app.test_client() as c:
            r = c.post("/reset_conversation", json={"channel": "discord"})
        assert r.get_json()["cleared_turns"] == 1
        assert len(ws.conversation_context()) == 2  # voice untouched

    def test_reset_route_empty_body_clears_all(self):
        ws.record_turn("a", "b")
        ws.record_turn("c", "d", channel="discord")
        with ws.app.test_client() as c:
            r = c.post("/reset_conversation")
        assert r.get_json()["cleared_turns"] == 2
        assert ws.conversation_context("discord") == []


class TestAnnounce:
    """Proactive notifications: Jarvis phrases an internal event and — crucially
    — records it into the channel's memory so a follow-up reply has context
    (the gap that made him forget alert DMs he'd sent)."""

    def setup_method(self):
        ws.reset_conversation()

    def teardown_method(self):
        ws.reset_conversation()

    def test_announce_frames_event_and_records_both_sides(self, monkeypatch):
        seen = {}

        def fake_think(text, channel="voice", use_tools=True):
            seen["text"], seen["channel"] = text, channel
            seen["use_tools"] = use_tools
            return "Heads up — your GPU is running hot."

        monkeypatch.setattr(ws, "think", fake_think)
        reply = ws.announce("GPUHot on pc_gpu: over 85C.", channel="discord")
        assert reply == "Heads up — your GPU is running hot."
        # The framing marks it as unprompted so Jarvis doesn't reply as if asked.
        assert ws.ANNOUNCE_FRAMING in seen["text"]
        assert "GPUHot" in seen["text"] and seen["channel"] == "discord"
        # An ALERT keeps its tools: "GPU is hot" may legitimately warrant
        # checking the current temperature. Only self-contained events opt out.
        assert seen["use_tools"] is True
        # Both sides land in memory: the trigger (compact marker, not the verbose
        # framing) and Jarvis's own words — so "what was that?" has context.
        conv = ws.conversation_context("discord")
        assert conv[-2]["role"] == "user" and "[system event]" in conv[-2]["content"]
        assert ws.ANNOUNCE_FRAMING not in conv[-2]["content"]
        assert conv[-1]["role"] == "assistant"
        assert conv[-1]["content"] == "Heads up — your GPU is running hot."

    def test_announce_route(self, monkeypatch):
        monkeypatch.setattr(
            ws, "think",
            lambda text, channel="voice", use_tools=True: "Notified.")
        with ws.app.test_client() as c:
            r = c.post("/announce",
                       json={"event": "TargetDown on pc_gpu.", "channel": "discord"})
        assert r.status_code == 200 and r.get_json()["reply"] == "Notified."
        assert ws.conversation_context("discord")[-1]["content"] == "Notified."

    def test_announce_can_opt_out_of_tools_for_self_contained_events(
            self, monkeypatch):
        """A fantasy write report (#029) describes something that already
        happened and is fully described in the event text, so a tool call is
        wrong by definition — ANNOUNCE_FRAMING already forbids inventing
        detail beyond what's given."""
        seen = {}

        def fake_think(text, channel="voice", use_tools=True):
            seen["use_tools"] = use_tools
            return "Swapped Skattebo in for Hall."

        monkeypatch.setattr(ws, "think", fake_think)
        ws.announce("WES changed the lineup.", channel="discord",
                    use_tools=False)
        assert seen["use_tools"] is False

    def test_announce_route_passes_use_tools_through(self, monkeypatch):
        seen = {}

        def fake_think(text, channel="voice", use_tools=True):
            seen["use_tools"] = use_tools
            return "Notified."

        monkeypatch.setattr(ws, "think", fake_think)
        with ws.app.test_client() as c:
            r = c.post("/announce", json={"event": "wrote the lineup",
                                          "use_tools": False})
        assert r.status_code == 200
        assert seen["use_tools"] is False

    def test_announce_route_defaults_to_tools_on(self, monkeypatch):
        """Existing callers (the alert watcher) send no flag and must be
        completely unaffected."""
        seen = {}

        def fake_think(text, channel="voice", use_tools=True):
            seen["use_tools"] = use_tools
            return "Notified."

        monkeypatch.setattr(ws, "think", fake_think)
        with ws.app.test_client() as c:
            c.post("/announce", json={"event": "TargetDown on pc_gpu."})
        assert seen["use_tools"] is True

    def test_announce_empty_event_is_400(self):
        with ws.app.test_client() as c:
            assert c.post("/announce", json={"event": "  "}).status_code == 400


class TestUsageLedger:
    """Token usage tracking: per-call CSV ledger + /usage rollup with the
    saved-vs-Claude estimate."""

    def _log(self, tmp_path, monkeypatch):
        path = str(tmp_path / "usage.csv")
        monkeypatch.setattr(ws, "USAGE_LOG", path)
        return path

    def test_record_and_summarize(self, tmp_path, monkeypatch):
        self._log(tmp_path, monkeypatch)
        ws.record_usage("gemma4:e4b", "router", "voice", 1_000_000, 0)
        ws.record_usage("gemma4:e4b", "router", "voice", 0, 1_000_000)
        ws.record_usage("gemma4:12b", "escalate", "discord", 500, 200)
        ws.record_usage("claude-haiku-4-5", "claude", "voice", 2_000_000, 0)
        s = ws.usage_summary()
        by = {(r["model"], r["source"]): r for r in s["by_call_site"]}
        router = by[("gemma4:e4b", "router")]
        assert router["calls"] == 2
        assert router["in_tokens"] == 1_000_000
        assert router["out_tokens"] == 1_000_000
        assert router["usd_at_haiku_rates"] == 6.0   # $1 in + $5 out
        assert router["local"] is True
        assert by[("claude-haiku-4-5", "claude")]["local"] is False
        assert s["local_saved_usd_estimate"] == round(6.0 + 0.0015, 4)
        assert s["claude_spent_usd"] == 2.0

    def test_zero_token_rows_are_dropped(self, tmp_path, monkeypatch):
        path = self._log(tmp_path, monkeypatch)
        ws.record_usage("gemma4:e4b", "router", "voice", 0, 0)
        ws.record_usage("gemma4:e4b", "router", "voice", None, None)
        assert not ws.usage_summary()["by_call_site"]
        import os
        assert not os.path.exists(path)  # nothing worth writing

    def test_days_window_filters_old_rows(self, tmp_path, monkeypatch):
        import csv as _csv
        path = self._log(tmp_path, monkeypatch)
        with open(path, "w", newline="") as f:
            w = _csv.writer(f)
            w.writerow(ws.USAGE_FIELDS)
            w.writerow(["2020-01-01 00:00:00", "gemma4:e4b", "router", "voice", 10, 10])
        ws.record_usage("gemma4:e4b", "router", "voice", 5, 5)
        assert ws.usage_summary()["by_call_site"][0]["calls"] == 2
        assert ws.usage_summary(days=1)["by_call_site"][0]["calls"] == 1

    def test_missing_ledger_is_empty_summary(self, tmp_path, monkeypatch):
        self._log(tmp_path, monkeypatch)
        s = ws.usage_summary()
        assert s["by_call_site"] == [] and s["claude_spent_usd"] == 0.0

    def test_stream_local_records_router_usage(self, tmp_path, monkeypatch):
        self._log(tmp_path, monkeypatch)
        fake, _ = TestOllamaBackend._fake_urlopen([[
            {"message": {"content": "Hi."}, "done": True,
             "prompt_eval_count": 120, "eval_count": 8}]])
        monkeypatch.setattr(ws.urllib.request, "urlopen", fake)
        "".join(ws._stream_local("hi", channel="discord"))
        row = ws.usage_summary()["by_call_site"][0]
        assert row["model"] == ws.LOCAL_LLM_MODEL and row["source"] == "router"
        assert row["in_tokens"] == 120 and row["out_tokens"] == 8

    def test_usage_endpoint(self, tmp_path, monkeypatch):
        self._log(tmp_path, monkeypatch)
        ws.record_usage("gemma4:12b", "vlm", "scene", 900, 60)
        with ws.app.test_client() as c:
            r = c.get("/usage")
        assert r.status_code == 200
        body = r.get_json()
        assert body["by_call_site"][0]["source"] == "vlm"
        assert "pricing_basis" in body

    def test_record_usage_increments_prometheus_counters(self, tmp_path,
                                                         monkeypatch):
        self._log(tmp_path, monkeypatch)
        labels = dict(direction="in", model="gemma4:e4b",
                      source="router", channel="voice")
        before = ws.TOKENS_TOTAL.labels(**labels)._value.get()
        ws.record_usage("gemma4:e4b", "router", "voice", 120, 8)
        assert ws.TOKENS_TOTAL.labels(**labels)._value.get() == before + 120
        out = ws.TOKENS_TOTAL.labels(**{**labels, "direction": "out"})
        assert out._value.get() >= 8
        # zero-token calls must not count
        calls = ws.CALLS_TOTAL.labels("gemma4:e4b", "router", "voice")
        n = calls._value.get()
        ws.record_usage("gemma4:e4b", "router", "voice", 0, 0)
        assert calls._value.get() == n

    def test_metrics_endpoint_exposes_token_counters(self, tmp_path,
                                                     monkeypatch):
        self._log(tmp_path, monkeypatch)
        ws.record_usage("gemma4:12b", "escalate", "discord", 500, 200)
        with ws.app.test_client() as c:
            r = c.get("/metrics")
        assert r.status_code == 200
        text = r.get_data(as_text=True)
        assert ('wes_llm_tokens_total{channel="discord",direction="in",'
                'model="gemma4:12b",source="escalate"}') in text
        assert "wes_llm_calls_total" in text


class TestTurnLog:
    """turns.jsonl: per-exchange content log with tool/escalation capture,
    a rolling size cap, and the GET /turns tail endpoint. (conftest's
    _sandbox_ledgers autouse fixture points TURNS_LOG at tmp.)"""

    def test_record_turn_logs_with_tools_and_escalation(self):
        ws._turn_begin()
        ws._note_tool("get_time")
        ws._note_escalation()
        ws.record_turn("what time is it", "It's noon.", channel="discord")
        rec = ws.recent_turns()[0]
        assert rec["transcript"] == "what time is it"
        assert rec["reply"] == "It's noon."
        assert rec["channel"] == "discord"
        assert rec["tools"] == ["get_time"]
        assert rec["escalated"] is True

    def test_notes_are_consumed_per_turn(self):
        ws._turn_begin()
        ws._note_tool("look")
        ws.record_turn("q1", "r1")
        ws.record_turn("q2", "r2")  # no new notes -> clean record
        newest = ws.recent_turns()[0]
        assert newest["transcript"] == "q2"
        assert newest["tools"] == [] and newest["escalated"] is False

    def test_notes_without_begin_are_ignored(self):
        # unit tests / stray threads may call _note_tool with no turn open
        ws._turn_notes.__dict__.clear()
        ws._note_tool("get_time")
        ws._note_escalation()
        ws.record_turn("q", "r")
        assert ws.recent_turns()[0]["tools"] == []

    def test_no_request_is_not_logged(self):
        # a true no-request (silence / empty transcript) isn't a turn at all
        ws.record_turn("", "Sorry, I didn't catch that.")
        ws.record_turn("   ", "reply")
        assert ws.recent_turns() == []

    def test_empty_reply_is_logged_as_a_failed_turn(self):
        # a REQUEST that produced no reply MUST still be logged for observability
        # (the "(no reply)" case) — tagged error, but NOT kept as memory
        ws.reset_conversation("discord")  # isolate from other tests' memory
        ws.record_turn("hard question", "", channel="discord")
        recs = ws.recent_turns()
        assert len(recs) == 1
        assert recs[0]["transcript"] == "hard question"
        assert recs[0]["error"] == "empty_reply"
        assert ws.conversation_context("discord") == []  # logged, not remembered

    def test_error_param_tags_the_logged_turn(self):
        ws.record_turn("q", "", error="RuntimeError: boom")
        assert ws.recent_turns()[0]["error"] == "RuntimeError: boom"

    def test_successful_turn_has_no_error_field(self):
        ws.record_turn("q", "a real answer")
        assert "error" not in ws.recent_turns()[0]

    def test_stream_local_tool_call_is_captured(self, monkeypatch):
        fake, _ = TestOllamaBackend._fake_urlopen([
            [{"message": {"tool_calls": [{"function": {
                "name": "get_time", "arguments": {}}}]}, "done": True}],
            [{"message": {"content": "It's noon."}, "done": True}]])
        monkeypatch.setattr(ws.urllib.request, "urlopen", fake)
        ws._turn_begin()
        reply = "".join(ws._stream_local("what time is it"))
        ws.record_turn("what time is it", reply)
        assert ws.recent_turns()[0]["tools"] == ["get_time"]

    def test_rolling_cap_trims_to_turns_max(self, monkeypatch):
        monkeypatch.setattr(ws, "TURNS_MAX", 5)
        monkeypatch.setattr(ws, "_TURNS_TRIM_BYTES", 200)
        for i in range(20):
            ws.record_turn(f"q{i}", f"r{i}")
        turns = ws.recent_turns(n=100)
        assert len(turns) <= 5
        assert turns[0]["transcript"] == "q19"  # newest survives the trim

    def test_turns_endpoint_n_and_channel_filter(self):
        ws.record_turn("from voice", "v", channel="voice")
        ws.record_turn("from discord", "d", channel="discord")
        with ws.app.test_client() as c:
            r = c.get("/turns?n=1&channel=voice")
        assert r.status_code == 200
        body = r.get_json()["turns"]
        assert len(body) == 1 and body[0]["transcript"] == "from voice"

    def test_missing_log_is_empty(self):
        with ws.app.test_client() as c:
            assert c.get("/turns").get_json()["turns"] == []


class TestDurableMemory:
    """Phase 1 (#012/#024): unified semantic memory (MEMORY.md) + persona
    (SOUL.md), injected into every channel's system prompt; remember/forget
    tools. (conftest sandboxes MEMORY_FILE/SOUL_FILE into tmp.)"""

    def test_remember_then_injected_into_prompt(self):
        out = ws.run_tool("remember", {"fact": "the user's dog is Biscuit"})
        assert "remember" in out.lower()
        # unified: shows up in EVERY channel's system prompt
        assert "Biscuit" in ws.system_prompt("voice")
        assert "Biscuit" in ws.system_prompt("discord")

    def test_remember_persists_across_reset(self):
        ws.run_tool("remember", {"fact": "favorite color is teal"})
        ws.reset_conversation()  # clears the window, NOT durable memory
        assert "teal" in ws.system_prompt("voice")

    def test_forget_removes_fact(self):
        ws.run_tool("remember", {"fact": "the cat is named Mittens"})
        ws.run_tool("remember", {"fact": "the dog is named Biscuit"})
        out = ws.run_tool("forget", {"match": "cat"})
        assert "forgotten" in out.lower()
        prompt = ws.system_prompt("voice")
        assert "Mittens" not in prompt and "Biscuit" in prompt

    def test_forget_unknown_is_graceful(self):
        assert "don't have anything" in ws.run_tool("forget", {"match": "xyz"}).lower()

    def test_empty_memory_injects_nothing(self):
        assert ws.memory_block() == ""

    def test_soul_falls_back_to_system_prompt_when_absent(self):
        # no SOUL.md in tmp -> behavior governed by the in-code SYSTEM_PROMPT
        assert ws.soul_prompt() == ws.SYSTEM_PROMPT

    def test_soul_file_overrides_persona(self, tmp_path, monkeypatch):
        p = tmp_path / "SOUL.md"
        p.write_text("You are Jarvis, dry and terse.", encoding="utf-8")
        monkeypatch.setattr(ws, "SOUL_FILE", str(p))
        assert ws.soul_prompt() == "You are Jarvis, dry and terse."
        assert "dry and terse" in ws.system_prompt("voice")

    def test_memory_is_size_capped(self, monkeypatch):
        monkeypatch.setattr(ws, "MEMORY_MAX_BYTES", 200)
        for i in range(60):
            ws.run_tool("remember", {"fact": f"fact number {i} padding padding"})
        block = ws.memory_block()
        assert len(block.encode("utf-8")) < 200 + 300  # header text + cap
        assert "fact number 59" in block          # newest kept
        assert "fact number 0 " not in block      # oldest dropped

    def test_tools_registered(self):
        names = [t["name"] for t in ws.TOOLS]
        assert "remember" in names and "forget" in names

    def test_discord_routes_through_deep_tier(self, monkeypatch):
        # #001 / higher-thinking: text channels run ESCALATE_MODEL + thinking as
        # their router; voice stays on the fast model. Capture the Ollama call.
        monkeypatch.setattr(ws, "ESCALATE_MODEL", "gemma4:12b")
        monkeypatch.setattr(ws, "DEEP_CHANNELS", {"discord"})
        fake, calls = TestOllamaBackend._fake_urlopen([[
            {"message": {"content": "ok"}, "done": True}]])
        monkeypatch.setattr(ws.urllib.request, "urlopen", fake)
        ws._think_local("hi", channel="discord")
        assert calls[0]["model"] == "gemma4:12b"      # deep model
        assert calls[0]["think"] is True              # thinking on

    def test_voice_stays_on_fast_router(self, monkeypatch):
        monkeypatch.setattr(ws, "ESCALATE_MODEL", "gemma4:12b")
        monkeypatch.setattr(ws, "DEEP_CHANNELS", {"discord"})
        fake, calls = TestOllamaBackend._fake_urlopen([[
            {"message": {"content": "ok"}, "done": True}]])
        monkeypatch.setattr(ws.urllib.request, "urlopen", fake)
        ws._think_local("hi", channel="voice")
        assert calls[0]["model"] == ws.LOCAL_LLM_MODEL   # fast e4b router
        assert calls[0]["model"] != "gemma4:12b"
        assert calls[0]["think"] is False

    def test_channel_deep_requires_escalate_model(self, monkeypatch):
        monkeypatch.setattr(ws, "DEEP_CHANNELS", {"discord"})
        monkeypatch.setattr(ws, "ESCALATE_MODEL", "")
        assert ws._channel_deep("discord") is False   # no deep model -> fast

    def test_tool_result_surfaced_when_model_goes_silent(self, monkeypatch):
        # round 1: call remember; round 2: model emits nothing. The turn must
        # not be silent — the tool's confirmation becomes the reply.
        fake, _ = TestOllamaBackend._fake_urlopen([
            [{"message": {"tool_calls": [{"function": {
                "name": "remember", "arguments": {"fact": "the sky is blue"}}}]},
              "done": True}],
            [{"message": {"content": ""}, "done": True}]])
        monkeypatch.setattr(ws.urllib.request, "urlopen", fake)
        reply = "".join(ws._stream_local("remember the sky is blue"))
        assert "remember" in reply.lower() and "sky is blue" in reply


class TestFantasyRosterMovesTool:
    """#035 — the roster tool. It gates an IRREVERSIBLE action, so the dispatch
    must be strict about what counts as 'yes'. Since 2026-08-01 approval names
    BOTH players rather than setting a flag: a flag authorises "whatever is top
    of the list right now", and that list really did shift between a suggestion
    and its approval once."""

    def _spy(self, monkeypatch, seen):
        monkeypatch.setattr(
            ws.wes_execute, "propose_roster_moves",
            lambda team=None, approve=None: seen.update(
                team=team, approve=approve) or "ok")

    def test_registered(self):
        assert "fantasy_roster_moves" in [t["name"] for t in ws.TOOLS]

    def test_defaults_to_recommend_only(self, monkeypatch):
        seen = {}
        self._spy(monkeypatch, seen)
        ws.run_tool("fantasy_roster_moves", {})
        assert seen["approve"] is None

    def test_a_complete_pair_is_passed_through(self, monkeypatch):
        seen = {}
        self._spy(monkeypatch, seen)
        ws.run_tool("fantasy_roster_moves",
                    {"approve": {"drop": "Jake Ferguson",
                                 "add": "Brenton Strange"}})
        assert seen["approve"] == {"drop": "Jake Ferguson",
                                   "add": "Brenton Strange"}

    @pytest.mark.parametrize("val", [
        True, "yes", 1, None, "", [], {},
        {"drop": "A"},                 # half filled in
        {"add": "B"},
        {"drop": "A", "add": ""},      # blank half
        {"drop": "", "add": "B"},
        "drop A add B",                # a sentence, not a pair
    ])
    def test_anything_that_is_not_a_complete_pair_recommends_instead(
            self, monkeypatch, val):
        """A stringy, truthy, or half-formed value must NOT trigger a permanent
        drop. The local 12b emitting something malformed should degrade to
        recommending, never to dropping a player."""
        seen = {}
        self._spy(monkeypatch, seen)
        ws.run_tool("fantasy_roster_moves", {"approve": val})
        assert seen["approve"] is None

    def test_schema_requires_both_halves(self):
        tool = next(t for t in ws.TOOLS if t["name"] == "fantasy_roster_moves")
        ap = tool["input_schema"]["properties"]["approve"]
        assert set(ap["required"]) == {"drop", "add"}

    def test_description_warns_the_drop_is_permanent(self):
        tool = next(t for t in ws.TOOLS if t["name"] == "fantasy_roster_moves")
        desc = tool["description"].lower()
        assert "permanent" in desc
        assert "only recommends" in desc
        # It must tell the model to name the players, not flip a switch.
        assert "approve" in desc and "never invent names" in desc


class TestWriteSuppressionHeader:
    """`X-WES-No-Writes` must reach wes_execute, and must not leak to the next
    request on a recycled thread (2026-08-14).

    Exercises the request hooks through test_request_context rather than
    registering probe routes: Flask forbids adding routes once the app has
    served its first request, so route-based probes pass alone and fail in a
    full run."""

    def teardown_method(self):
        ws.wes_execute.set_writes_suppressed(False)

    def _suppressed_for(self, headers):
        with ws.app.test_request_context("/", headers=headers):
            ws._apply_write_suppression()
            return ws.wes_execute.writes_suppressed()

    def test_header_suppresses_for_that_request(self):
        assert self._suppressed_for({"X-WES-No-Writes": "1"}) is True

    def test_absent_header_does_not_suppress(self):
        assert self._suppressed_for({}) is False

    def test_it_is_cleared_after_the_request(self):
        """Threads are pooled; a suppression that outlived its request would
        silently turn the NEXT caller's real write into a dry run."""
        ws.app.config["TESTING"] = True
        with ws.app.test_client() as c:
            c.get("/health", headers={"X-WES-No-Writes": "1"})
        assert ws.wes_execute.writes_suppressed() is False

    @pytest.mark.parametrize("val,expected", [
        ("1", True), ("true", True), ("yes", True), ("anything", True),
        ("0", False), ("false", False), ("", False),
    ])
    def test_malformed_values_fail_SAFE(self, val, expected):
        """Anything present that isn't an explicit off-value suppresses.
        Over-suppressing costs a dry run; under-suppressing writes to a real
        account."""
        assert self._suppressed_for({"X-WES-No-Writes": val}) is expected
