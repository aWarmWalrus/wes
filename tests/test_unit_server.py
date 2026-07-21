"""Fast unit tests for pure server logic — no network, no API, no model loads.

Run: cd Z:\\wes\\tests && python -m pytest        (from the wes-pc venv)
These are the regression tripwires for the server's non-LLM logic.
"""
import json
import re
import threading
import time

import wes_server as ws


class TestVlmPrompt:
    def test_no_identities_returns_base(self):
        assert ws._vlm_prompt(None) == ws.VLM_PROMPT
        assert ws._vlm_prompt([]) == ws.VLM_PROMPT

    def test_unknown_only_returns_base(self):
        assert ws._vlm_prompt([{"name": "unknown", "position": "center"}]) == ws.VLM_PROMPT

    def test_known_person_woven_in(self):
        p = ws._vlm_prompt([{"name": "charlie", "clothing": "orange", "position": "center"}])
        assert "charlie" in p and "orange" in p and "center" in p
        assert p != ws.VLM_PROMPT

    def test_dict_coerced_to_list(self):
        p = ws._vlm_prompt({"name": "cindy", "clothing": "blue", "position": "left"})
        assert "cindy" in p and "blue" in p

    def test_multiple_people_get_disambiguation_hint(self):
        p = ws._vlm_prompt([
            {"name": "charlie", "clothing": "orange", "position": "center"},
            {"name": "cindy", "clothing": "blue", "position": "left"},
        ])
        assert "charlie" in p and "cindy" in p
        assert "clothing" in p.lower() and "apart" in p.lower()

    def test_garbage_input_is_safe(self):
        assert ws._vlm_prompt("not a list") == ws.VLM_PROMPT
        assert ws._vlm_prompt([1, 2, 3]) == ws.VLM_PROMPT  # non-dict items filtered out


class TestNextSentence:
    def test_splits_on_terminator_space(self):
        s, rest = ws.next_sentence("Hello there. How are")
        assert s == "Hello there." and rest == "How are"

    def test_no_complete_sentence(self):
        s, rest = ws.next_sentence("Hello there")
        assert s is None and rest == "Hello there"

    def test_question_and_exclamation(self):
        assert ws.next_sentence("Really? yes")[0] == "Really?"
        assert ws.next_sentence("Wow! ok")[0] == "Wow!"

    def test_decimal_is_not_split(self):
        # "3.5" has no space after the '.', so the split must land after "degrees."
        s, _ = ws.next_sentence("It is 3.5 degrees. Warm")
        assert s == "It is 3.5 degrees."


class TestNorm:
    def test_lowercases_and_strips_punct(self):
        assert ws._norm("What's the TIME?!") == "whats the time"

    def test_all_punct_is_empty(self):
        assert ws._norm("...") == ""


class TestSpeculationLookup:
    def setup_method(self):
        ws._spec_cache.clear()

    def _put(self, transcript, reply):
        ev = threading.Event()
        ev.set()
        ws._spec_cache[ws._norm(transcript)] = {
            "reply": reply, "event": ev, "ts": time.time(),
        }

    def test_exact_match(self):
        self._put("what time is it", "It's noon.")
        assert ws.lookup_speculation("What time is it?") == "It's noon."

    def test_prefix_match_above_ratio(self):
        self._put("what is the weather today", "Sunny.")
        assert ws.lookup_speculation("what is the weather today ok") == "Sunny."

    def test_no_match_below_ratio(self):
        self._put("what", "X")
        assert ws.lookup_speculation("what is the weather like outside today") is None

    def test_empty_transcript(self):
        assert ws.lookup_speculation("") is None


class TestRateLimitBudget:
    def setup_method(self):
        with ws._rate_lock:
            ws._rate_calls.clear()

    def test_budget_ok_when_idle(self):
        assert ws._spec_budget_ok() is True

    def test_budget_exhausted_at_reserve(self):
        for _ in range(max(0, ws._RATE_LIMIT_RPM - ws._SPEC_RESERVE)):
            ws._record_llm_call()
        assert ws._spec_budget_ok() is False

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
        assert "10.0.0.168" in out and "10.0.0.79" in out  # from hosts.yaml

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


class TestDescribeSceneCache:
    def _prime(self, desc, faces, age=0.0):
        now = time.time() - age
        with ws._scene_lock:
            ws._scene_cache.update(desc=desc, ts=now, faces=faces, faces_ts=now)

    def test_fresh_cache_hit_includes_people(self):
        self._prime("A test scene.", [
            {"name": "charlie", "position": "center", "clothing": "orange"}])
        out = ws.describe_scene()
        assert out["description"] == "A test scene."
        assert out["recognition_ran"] is True
        assert out["people"][0]["name"] == "charlie"

    def test_stale_cache_is_not_returned(self, monkeypatch):
        self._prime("Old stale scene.", [], age=ws.SCENE_TTL + 5)

        def boom(*a, **k):
            raise OSError("network disabled in test")

        monkeypatch.setattr(ws.urllib.request, "urlopen", boom)
        try:
            result = ws.describe_scene()  # stale -> tries to capture (which we block)
        except OSError:
            result = None
        assert result is None or result.get("description") != "Old stale scene."


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
        ws._conv_last["voice"] = time.time() - ws.CONV_TTL - 1
        assert ws.conversation_context() == []

    def test_channels_are_isolated(self):
        ws.record_turn("voice question", "voice answer")
        ws.record_turn("remote question", "remote answer", channel="discord")
        assert [m["content"] for m in ws.conversation_context()] == \
            ["voice question", "voice answer"]
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

    def test_interrupted_reply_is_tagged(self):
        ws.record_spoken_turn("tell me a story", "Once upon a time", False)
        ctx = ws.conversation_context()
        assert ctx[1]["content"] == "Once upon a time" + ws.INTERRUPT_TAG

    def test_completed_reply_is_not_tagged(self):
        ws.record_spoken_turn("hi", "Hello there.", True)
        assert ws.conversation_context()[1]["content"] == "Hello there."

    def test_interrupted_before_any_audio_records_nothing(self):
        ws.record_spoken_turn("tell me a story", "", False)
        assert ws.conversation_context() == []

    def test_roles_always_alternate_user_first(self):
        # the Anthropic API rejects non-alternating roles; the window cap is
        # in whole exchanges so this must hold for any history state
        for i in range(ws.CONV_TURNS + 5):
            ws.record_turn(f"q{i}", f"a{i}")
        ctx = ws.conversation_context()
        assert [m["role"] for m in ctx[0::2]] == ["user"] * (len(ctx) // 2)
        assert [m["role"] for m in ctx[1::2]] == ["assistant"] * (len(ctx) // 2)

    # --- Phase 0 (#023): per-channel depth + persistence -------------------

    def test_discord_window_is_deeper_than_voice(self):
        dmax = ws._conv_policy("discord")[0]
        vmax = ws._conv_policy("voice")[0]
        assert dmax > vmax
        # Discord keeps far more than the voice depth; voice is capped tight.
        for i in range(vmax + 10):
            ws.record_turn(f"dq{i}", f"da{i}", channel="discord")
            ws.record_turn(f"vq{i}", f"va{i}", channel="voice")
        assert len(ws.conversation_context("discord")) == 2 * (vmax + 10)
        assert len(ws.conversation_context("voice")) == 2 * vmax

    def test_discord_ttl_outlives_voice_ttl(self):
        # a gap that would expire voice must NOT expire discord
        ws.record_turn("q", "a", channel="discord")
        ws._conv_last["discord"] = time.time() - ws.CONV_TTL - 1
        assert ws.conversation_context("discord") != []

    def test_no_bleed_discord_content_absent_from_voice(self):
        ws.record_turn("secret discord thing", "ok", channel="discord")
        joined = " ".join(m["content"] for m in ws.conversation_context("voice"))
        assert "secret discord thing" not in joined
        assert ws.conversation_context("voice") == []

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
        ws.record_turn("old", "reply", channel="voice")
        path = ws._conv_file("voice")
        # age the file past the voice TTL
        old = time.time() - ws.CONV_TTL - 10
        os.utime(path, (old, old))
        ws._convs.clear()
        ws._conv_last.clear()
        ws.load_conversations()
        assert ws.conversation_context("voice") == []

    def test_reset_removes_persisted_file(self):
        import os
        ws.record_turn("a", "b", channel="discord")
        assert os.path.exists(ws._conv_file("discord"))
        ws.reset_conversation("discord")
        assert not os.path.exists(ws._conv_file("discord"))


class TestSttBias:
    """Contextual-biasing prompt for whisper (lexicon + conversation tail)."""

    def setup_method(self):
        ws.reset_conversation()

    def teardown_method(self):
        ws.reset_conversation()

    def test_lexicon_names_present(self):
        p = ws.stt_bias_prompt()
        for word in ("Jarvis", "Hailo", "Hue", "ecobee", "Matcha",
                     "Charlie", "Cindy", "Kaia", "Ellis", "Brooklyn", "NBA"):
            assert word in p

    def test_recent_conversation_is_appended(self):
        ws.record_turn("tell me about the moon", "The moon has no atmosphere.")
        p = ws.stt_bias_prompt()
        assert "moon" in p
        assert p.index("Jarvis") < p.index("moon")  # lexicon first

    def test_prompt_bounded_even_with_long_turns(self):
        # overlong prompts make whisper hallucinate; the conversation tail
        # must be truncated, never the lexicon
        ws.record_turn("x " * 500, "y " * 500)
        p = ws.stt_bias_prompt()
        assert len(p) <= len(ws.STT_LEXICON) + 301
        assert "Ellis" in p


class TestOllamaBackend:
    """Local (Ollama) LLM backend — schema conversion, tool loop, fallback.
    All network-free: urlopen is monkeypatched."""

    def test_tool_schema_conversion(self):
        out = ws._ollama_tools()
        assert len(out) == len(ws.TOOLS)
        names = [t["function"]["name"] for t in out]
        assert "describe_scene" in names and "get_datetime" in names
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

    def test_local_failure_falls_back_to_claude(self, monkeypatch):
        monkeypatch.setattr(ws, "LLM_BACKEND", "local")

        def boom(transcript):
            raise OSError("ollama down")
            yield  # pragma: no cover — makes this a generator

        monkeypatch.setattr(ws, "_stream_local", boom)
        monkeypatch.setattr(ws, "_stream_claude", lambda t: iter(["claude reply"]))
        assert "".join(ws.stream_reply("hi")) == "claude reply"

    def test_midstream_failure_does_not_restart(self, monkeypatch):
        monkeypatch.setattr(ws, "LLM_BACKEND", "local")

        def partial(transcript):
            yield "First half"
            raise OSError("connection dropped")

        monkeypatch.setattr(ws, "_stream_local", partial)
        monkeypatch.setattr(
            ws, "_stream_claude",
            lambda t: (_ for _ in ()).throw(AssertionError("must not fall back")))
        out = "".join(ws.stream_reply("hi"))
        assert out.startswith("First half") and "lost my train of thought" in out

    def test_spec_budget_unlimited_when_local(self, monkeypatch):
        monkeypatch.setattr(ws, "LLM_BACKEND", "local")
        for _ in range(ws._RATE_LIMIT_RPM + 5):
            ws._record_llm_call()
        try:
            assert ws._spec_budget_ok() is True
        finally:
            with ws._rate_lock:
                ws._rate_calls.clear()


class TestEscalation:
    """Smart routing: the local model calls escalate_to_claude to hand off."""

    def test_toolset_includes_escalation_when_claude_available(self, monkeypatch):
        monkeypatch.setattr(ws, "ESCALATE", True)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        names = [t["function"]["name"] for t in ws._local_toolset()]
        assert "escalate_to_claude" in names
        # ...but never in the shared TOOLS Claude itself sees.
        assert all(t["name"] != "escalate_to_claude" for t in ws.TOOLS)

    def test_toolset_omits_escalation_without_key(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        names = [t["function"]["name"] for t in ws._local_toolset()]
        assert "escalate_to_claude" not in names

    def test_toolset_omits_escalation_when_disabled(self, monkeypatch):
        monkeypatch.setattr(ws, "ESCALATE", False)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        names = [t["function"]["name"] for t in ws._local_toolset()]
        assert "escalate_to_claude" not in names


class TestWebSearch:
    """search_web: the router hands a LIVE-INFO query to Haiku + web search.
    Distinct from escalate_to_claude (hard reasoning -> local 12b+thinking)."""

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
        # carry escalate_to_claude — it already IS the reasoning escalation.
        monkeypatch.setattr(ws, "WEB_SEARCH", True)
        monkeypatch.setattr(ws, "ESCALATE", True)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        names = [t["function"]["name"] for t in ws._local_toolset(deep=True)]
        assert "search_web" in names
        assert "escalate_to_claude" not in names

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
                {"function": {"name": "escalate_to_claude",
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

    def test_escalation_ack_is_a_flushable_sentence(self):
        # The ack must end terminator+space or the TTS splitter would hold it
        # until Claude's first token, defeating its purpose.
        sent, rest = ws.next_sentence(ws.ESCALATE_ACK)
        assert sent is not None and rest == ""

    def test_escalation_ack_disabled_by_empty_string(self, monkeypatch):
        fake, _ = TestOllamaBackend._fake_urlopen([
            [{"message": {"content": "", "tool_calls": [
                {"function": {"name": "escalate_to_claude", "arguments": {}}}]},
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
        assert "escalate_to_claude" in names

    def test_escalation_hands_off_to_local_deep_model(self, monkeypatch):
        fake, calls = TestOllamaBackend._fake_urlopen([
            [{"message": {"content": "", "tool_calls": [
                {"function": {"name": "escalate_to_claude",
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
        assert "escalate_to_claude" not in names and "describe_scene" in names
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
                 {"function": {"name": "escalate_to_claude", "arguments": {}}}]},
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
                {"function": {"name": "escalate_to_claude", "arguments": {}}}]},
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
                 {"function": {"name": "escalate_to_claude", "arguments": {}}}]},
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


class TestTtsClean:
    """Markdown/symbol stripping so piper never reads 'asterisk asterisk'."""

    def test_plain_text_untouched(self):
        assert ws.tts_clean("It's 9:32 AM on July 4th.") == "It's 9:32 AM on July 4th."

    def test_emphasis_stripped(self):
        assert ws.tts_clean("the answer is **7pm**, not *6pm*") == \
            "the answer is 7pm, not 6pm"

    def test_link_keeps_text(self):
        assert ws.tts_clean("see [the docs](https://x.y/z) for more") == \
            "see the docs for more"

    def test_lists_and_headings_stripped(self):
        out = ws.tts_clean("## Steps\n- first thing\n2. second thing")
        assert out == "Steps\nfirst thing\nsecond thing"

    def test_code_fence_and_inline_code(self):
        assert ws.tts_clean("run ```bash\nls -la\n``` or `pwd`") == \
            "run \nls -la\n or pwd"

    def test_symbols_translated(self):
        assert ws.tts_clean("A → B ✓") == "A to B"

    def test_jr_sr_spoken_out(self):
        # piper reads "Jr." as "J R dot" -> expand to the spoken word
        assert ws.tts_clean("Mikel Brown Jr. had 20 points") == \
            "Mikel Brown Junior had 20 points"
        assert ws.tts_clean("Tim Hardaway Jr and Gary Sr") == \
            "Tim Hardaway Junior and Gary Senior"

    def test_ack_survives_cleaning(self):
        # The escalation ack goes through the same path; it must not vanish.
        assert ws.tts_clean(ws.ESCALATE_ACK) == ws.ESCALATE_ACK.strip()


class TestChannelSystemPrompt:
    """Text channels override the voice framing so the model never says
    'voice command' to someone typing on Discord."""

    def test_voice_channel_gets_the_voice_note(self):
        p = ws.system_prompt("voice")
        assert p.startswith(ws.SYSTEM_PROMPT)  # channel-agnostic persona first
        assert ws.VOICE_CHANNEL_NOTE in p
        assert ws.TEXT_CHANNEL_NOTE not in p

    def test_text_channels_get_the_text_note(self):
        for ch in ("discord", "text"):
            p = ws.system_prompt(ch)
            assert ws.TEXT_CHANNEL_NOTE in p
            assert ws.VOICE_CHANNEL_NOTE not in p
            assert p.startswith(ws.SYSTEM_PROMPT)  # same persona, different note

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

        def fake_think(text, channel="voice"):
            seen["text"], seen["channel"] = text, channel
            return "Heads up — your GPU is running hot."

        monkeypatch.setattr(ws, "think", fake_think)
        reply = ws.announce("GPUHot on pc_gpu: over 85C.", channel="discord")
        assert reply == "Heads up — your GPU is running hot."
        # The framing marks it as unprompted so Jarvis doesn't reply as if asked.
        assert ws.ANNOUNCE_FRAMING in seen["text"]
        assert "GPUHot" in seen["text"] and seen["channel"] == "discord"
        # Both sides land in memory: the trigger (compact marker, not the verbose
        # framing) and Jarvis's own words — so "what was that?" has context.
        conv = ws.conversation_context("discord")
        assert conv[-2]["role"] == "user" and "[system event]" in conv[-2]["content"]
        assert ws.ANNOUNCE_FRAMING not in conv[-2]["content"]
        assert conv[-1]["role"] == "assistant"
        assert conv[-1]["content"] == "Heads up — your GPU is running hot."

    def test_announce_route(self, monkeypatch):
        monkeypatch.setattr(ws, "think",
                            lambda text, channel="voice": "Notified.")
        with ws.app.test_client() as c:
            r = c.post("/announce",
                       json={"event": "TargetDown on pc_gpu.", "channel": "discord"})
        assert r.status_code == 200 and r.get_json()["reply"] == "Notified."
        assert ws.conversation_context("discord")[-1]["content"] == "Notified."

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

    def test_empty_turns_are_not_logged(self):
        ws.record_turn("", "Sorry, I didn't catch that.")
        ws.record_turn("hello", "  ")
        assert ws.recent_turns() == []

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


class TestFaceSummary:
    def test_no_data(self):
        out = ws._face_summary(None)
        assert out == {"recognition_ran": False, "people": []}

    def test_people_summarized(self):
        out = ws._face_summary([
            {"name": "cindy", "position": "left", "clothing": "blue", "similarity": 0.9}])
        assert out["recognition_ran"] is True
        assert out["people"] == [
            {"name": "cindy", "position": "left", "clothing": "blue"}]


class TestSceneContext:
    def _prime_faces(self, faces, age=0.0):
        with ws._scene_lock:
            ws._scene_cache["faces"] = faces
            ws._scene_cache["faces_ts"] = time.time() - age

    def test_no_data_is_empty(self):
        self._prime_faces(None)
        assert ws._scene_context() == ""

    def test_stale_data_is_empty(self):
        self._prime_faces([{"name": "charlie"}], age=ws.SCENE_TTL + 5)
        assert ws._scene_context() == ""

    def test_known_person_present(self):
        self._prime_faces([
            {"name": "charlie", "position": "center", "clothing": "orange"}])
        ctx = ws._scene_context()
        assert "charlie" in ctx and "center" in ctx and "orange" in ctx
        # The behavioral instruction: assume listed people ARE in view.
        assert "ARE in view" in ctx

    def test_unknown_only(self):
        self._prime_faces([{"name": "unknown", "position": "left", "clothing": "gray"}])
        ctx = ws._scene_context()
        assert "none match" in ctx and "charlie" not in ctx

    def test_nobody_in_frame(self):
        self._prime_faces([])
        assert "no people" in ws._scene_context()

    def test_mixed_known_and_unknown(self):
        self._prime_faces([
            {"name": "charlie", "position": "center", "clothing": "orange"},
            {"name": "unknown", "position": "left", "clothing": "gray"},
        ])
        ctx = ws._scene_context()
        assert "charlie" in ctx and "1 unrecognized" in ctx
