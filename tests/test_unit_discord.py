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
    """Pure logic of the Prometheus alert poller: response parsing, keying
    (per rule+instance), and fire/resolve message formatting."""

    PAYLOAD = {"status": "success", "data": {"result": [
        {"metric": {"alertname": "TargetDown", "alertstate": "firing",
                    "instance": "10.0.0.168:9835", "job": "pc_gpu"}},
        {"metric": {"alertname": "TargetDown", "alertstate": "firing",
                    "instance": "10.0.0.168:9182", "job": "pc_windows"}},
        {"metric": {"alertname": "GPUHot", "alertstate": "firing",
                    "instance": "10.0.0.168:9835", "job": "pc_gpu"}},
    ]}}

    def test_parse_keys_by_rule_and_instance(self):
        alerts = wd.parse_alerts(self.PAYLOAD)
        assert len(alerts) == 3  # same rule on two targets = two alerts
        assert alerts["TargetDown|10.0.0.168:9835"] == "TargetDown [pc_gpu]"

    def test_parse_empty_result(self):
        assert wd.parse_alerts({"data": {"result": []}}) == {}
        assert wd.parse_alerts({}) == {}

    def test_messages_fire_and_resolve(self):
        msgs = wd.alert_messages({"k": "GPUHot [pc_gpu]"},
                                 {"j": "TargetDown [pc_windows]"})
        assert msgs == ["🚨 WES alert: GPUHot [pc_gpu]",
                        "✅ Resolved: TargetDown [pc_windows]"]

    def test_steady_state_produces_no_messages(self):
        cur = wd.parse_alerts(self.PAYLOAD)
        fired = {k: v for k, v in cur.items() if k not in cur}
        assert wd.alert_messages(fired, {}) == []

    def test_fetch_queries_firing_alerts(self, monkeypatch):
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
        alerts = wd.fetch_firing_alerts()
        assert len(alerts) == 3
        assert calls[0].startswith(wd.PROM_URL + "/api/v1/query?query=")
        assert "alertstate" in calls[0]

    def test_watcher_survives_cp1252_stdout(self, monkeypatch, capsys):
        """Regression (2026-07-05): under the scheduled task stdout is cp1252,
        and printing the DM's emoji raised UnicodeEncodeError — the except
        block re-raised printing the repr and the watcher died silently on
        the first real alert. All console lines must be ASCII-safe."""
        import asyncio
        import builtins

        seq = [{}, {"TargetDown|x": "TargetDown [pc_gpu]"}, {}]
        n = {"i": 0}

        def fake_fetch():
            i = min(n["i"], len(seq) - 1)
            n["i"] += 1
            return seq[i]

        real_print = builtins.print

        def cp1252_print(*args, **kw):
            for a in args:
                str(a).encode("cp1252")  # raises exactly like the task's stdout
            real_print(*args, **kw)

        monkeypatch.setattr(wd, "fetch_firing_alerts", fake_fetch)
        monkeypatch.setattr(wd, "ALERT_POLL_S", 0.001)
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
        assert any("🚨" in m and "TargetDown" in m for m in sent)   # fired DM
        assert any(m.startswith("✅") for m in sent)                # resolved DM
