"""Unit tests for the Discord frontend bridge (pc/wes_discord.py).

Network-free: the discord package is never imported (the module keeps all
discord.py usage behind make_client/main), and HTTP goes through a
monkeypatched urlopen. The LLM behavior behind /respond_text is covered by
the server unit tests; these cover the bridge's own logic — the auth
allowlist, message routing, reply chunking, and the server round-trip.
"""
import io
import json

import wes_discord as wd


class TestAuthAllowlist:
    def test_owner_only(self, monkeypatch):
        monkeypatch.setattr(wd, "OWNER_ID", 42)
        assert wd.authorized(42) is True
        assert wd.authorized(43) is False

    def test_unset_owner_fails_closed(self, monkeypatch):
        # OWNER_ID=0 (env unset) must authorize NOBODY, including id 0.
        monkeypatch.setattr(wd, "OWNER_ID", 0)
        assert wd.authorized(0) is False
        assert wd.authorized(42) is False


class TestShouldHandle:
    def setup_method(self):
        self.owner = 42

    def test_owner_dm_handled(self, monkeypatch):
        monkeypatch.setattr(wd, "OWNER_ID", self.owner)
        assert wd.should_handle(42, is_self=False, is_dm=True, mentions_bot=False)

    def test_guild_needs_mention(self, monkeypatch):
        monkeypatch.setattr(wd, "OWNER_ID", self.owner)
        assert not wd.should_handle(42, is_self=False, is_dm=False, mentions_bot=False)
        assert wd.should_handle(42, is_self=False, is_dm=False, mentions_bot=True)

    def test_stranger_ignored_even_when_mentioning(self, monkeypatch):
        monkeypatch.setattr(wd, "OWNER_ID", self.owner)
        assert not wd.should_handle(7, is_self=False, is_dm=True, mentions_bot=True)

    def test_own_messages_ignored(self, monkeypatch):
        # The bot must never answer itself (reply loop).
        monkeypatch.setattr(wd, "OWNER_ID", self.owner)
        assert not wd.should_handle(42, is_self=True, is_dm=True, mentions_bot=False)


class TestChunkReply:
    def test_short_reply_is_one_chunk(self):
        assert wd.chunk_reply("hello") == ["hello"]

    def test_long_reply_splits_under_limit(self):
        text = "word " * 1000  # 5000 chars
        chunks = wd.chunk_reply(text)
        assert all(len(c) <= wd.DISCORD_MSG_LIMIT for c in chunks)
        assert " ".join(chunks).split() == text.split()  # nothing lost

    def test_prefers_whitespace_break(self):
        text = "a" * 1990 + " " + "b" * 100
        chunks = wd.chunk_reply(text)
        assert chunks[0] == "a" * 1990 and chunks[1] == "b" * 100

    def test_unbreakable_text_hard_splits(self):
        chunks = wd.chunk_reply("x" * 4500)
        assert [len(c) for c in chunks] == [2000, 2000, 500]

    def test_empty_reply(self):
        assert wd.chunk_reply("   ") == []


class TestServerRoundTrip:
    @staticmethod
    def _fake_urlopen(payload, calls):
        class FakeResp(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake(req, timeout=None):
            calls.append((req.full_url, json.loads(req.data)))
            return FakeResp(json.dumps(payload).encode())

        return fake

    def test_ask_server_posts_discord_channel(self, monkeypatch):
        calls = []
        monkeypatch.setattr(wd.urllib.request, "urlopen",
                            self._fake_urlopen({"reply": "hi there"}, calls))
        assert wd.ask_server("hello") == "hi there"
        url, body = calls[0]
        assert url.endswith("/respond_text")
        assert body == {"text": "hello", "channel": "discord"}

    def test_reset_scopes_to_discord_channel(self, monkeypatch):
        calls = []
        monkeypatch.setattr(wd.urllib.request, "urlopen",
                            self._fake_urlopen({"cleared_turns": 3}, calls))
        assert wd.reset_server() == 3
        url, body = calls[0]
        assert url.endswith("/reset_conversation")
        assert body == {"channel": "discord"}  # never the voice channel


class TestAlertWatcher:
    """The Prometheus alert poller: parsing /api/v1/alerts, keying per
    rule+instance, building the event Jarvis explains, and the raw fallback."""

    PAYLOAD = {"status": "success", "data": {"alerts": [
        {"labels": {"alertname": "TargetDown", "instance": "10.0.0.168:9835",
                    "job": "pc_gpu"},
         "annotations": {"summary": "Metrics from pc_gpu missing for 5 minutes."},
         "state": "firing", "value": "0"},
        {"labels": {"alertname": "TargetDown", "instance": "10.0.0.168:9182",
                    "job": "pc_windows"},
         "annotations": {"summary": "Metrics from pc_windows missing 5 minutes."},
         "state": "firing", "value": "0"},
        {"labels": {"alertname": "GPUHot", "instance": "10.0.0.168:9835",
                    "job": "pc_gpu"},
         "annotations": {"summary": "PC GPU over 85C for 5 minutes (now 88C)."},
         "state": "firing", "value": "88"},
        {"labels": {"alertname": "PiHot", "instance": "10.0.0.79:9100",
                    "job": "node"},
         "annotations": {"summary": "not firing yet"},
         "state": "pending", "value": "81"},
    ]}}

    def test_parse_keys_by_rule_and_instance(self):
        alerts = wd.parse_alerts(self.PAYLOAD)
        assert len(alerts) == 3  # same rule on two targets = two; pending dropped
        gpu = alerts["TargetDown|10.0.0.168:9835"]
        assert gpu["alertname"] == "TargetDown" and gpu["job"] == "pc_gpu"
        assert "missing for 5 minutes" in gpu["summary"]

    def test_parse_ignores_pending(self):
        assert "PiHot|10.0.0.79:9100" not in wd.parse_alerts(self.PAYLOAD)

    def test_parse_empty(self):
        assert wd.parse_alerts({"data": {"alerts": []}}) == {}
        assert wd.parse_alerts({}) == {}

    def test_describe_event_grounds_in_context(self):
        info = wd.parse_alerts(self.PAYLOAD)["GPUHot|10.0.0.168:9835"]
        ev = wd.describe_event(info, resolved=False)
        assert "STARTED FIRING" in ev and "over 85C" in ev
        assert "RTX 5060 Ti" in ev  # ALERT_CONTEXT background is included
        res = wd.describe_event(info, resolved=True)
        assert "CLEARED" in res

    def test_raw_summary_fallback_has_emoji(self):
        info = wd.parse_alerts(self.PAYLOAD)["GPUHot|10.0.0.168:9835"]
        assert wd.raw_summary(info).startswith("🚨")
        assert wd.raw_summary(info, resolved=True).startswith("✅")

    def test_fetch_uses_alerts_endpoint(self, monkeypatch):
        calls = []

        class FakeResp(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake(url, timeout=None):
            calls.append(url)
            return FakeResp(json.dumps(self.PAYLOAD).encode())

        monkeypatch.setattr(wd.urllib.request, "urlopen", fake)
        assert len(wd.fetch_firing_alerts()) == 3
        assert calls[0] == wd.PROM_URL + "/api/v1/alerts"

    def test_explain_event_posts_announce(self, monkeypatch):
        calls = []

        class FakeResp(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake(req, timeout=None):
            calls.append((req.full_url, json.loads(req.data)))
            return FakeResp(json.dumps({"reply": "Heads up, GPU is hot."}).encode())

        monkeypatch.setattr(wd.urllib.request, "urlopen", fake)
        info = wd.parse_alerts(self.PAYLOAD)["GPUHot|10.0.0.168:9835"]
        assert wd.explain_event(info) == "Heads up, GPU is hot."
        url, body = calls[0]
        assert url.endswith("/announce")
        assert body["channel"] == "discord"
        assert "STARTED FIRING" in body["event"]

    def test_watcher_explains_then_falls_back(self, monkeypatch):
        """Fired alert is phrased by the server; if the server is down the DM
        falls back to the raw summary so the alert is never lost."""
        import asyncio

        seq = [{}, wd.parse_alerts(self.PAYLOAD), {}]
        n = {"i": 0}

        def fake_fetch():
            i = min(n["i"], len(seq) - 1)
            n["i"] += 1
            return seq[i]

        def boom(info, resolved=False):
            raise OSError("server down")

        monkeypatch.setattr(wd, "fetch_firing_alerts", fake_fetch)
        monkeypatch.setattr(wd, "explain_event", boom)
        monkeypatch.setattr(wd, "ALERT_POLL_S", 0.001)

        sent = []

        class FakeUser:
            async def send(self, t):
                sent.append(t)

        class FakeClient:
            async def wait_until_ready(self):
                pass

            def is_closed(self):
                return n["i"] > 4

            async def fetch_user(self, uid):
                return FakeUser()

        asyncio.run(wd.alert_watch(FakeClient()))
        # three alerts fired then resolved -> raw fallback for all six
        assert any(m.startswith("🚨") and "GPU" in m for m in sent)
        assert any(m.startswith("✅") for m in sent)

    def test_watcher_survives_cp1252_stdout(self, monkeypatch):
        """Regression (2026-07-05): under the scheduled task stdout is cp1252,
        and printing the DM's emoji raised UnicodeEncodeError — the watcher
        died silently on the first real alert. All console lines must be
        ASCII-safe (the code logs with !a)."""
        import asyncio
        import builtins

        seq = [{}, wd.parse_alerts(self.PAYLOAD), {}]
        n = {"i": 0}

        def fake_fetch():
            i = min(n["i"], len(seq) - 1)
            n["i"] += 1
            return seq[i]

        # server reachable, returns emoji-laden natural text (worst case for
        # an ASCII console)
        monkeypatch.setattr(wd, "fetch_firing_alerts", fake_fetch)
        monkeypatch.setattr(wd, "explain_event",
                            lambda info, resolved=False: "🚨 GPU is hot, heads up.")
        monkeypatch.setattr(wd, "ALERT_POLL_S", 0.001)

        real_print = builtins.print

        def cp1252_print(*args, **kw):
            for a in args:
                str(a).encode("cp1252")  # raises exactly like the task's stdout
            real_print(*args, **kw)

        monkeypatch.setattr(builtins, "print", cp1252_print)

        sent = []

        class FakeUser:
            async def send(self, t):
                sent.append(t)

        class FakeClient:
            async def wait_until_ready(self):
                pass

            def is_closed(self):
                return n["i"] > 4

            async def fetch_user(self, uid):
                return FakeUser()

        asyncio.run(wd.alert_watch(FakeClient()))
        assert any("🚨" in m for m in sent)  # DMs still delivered
