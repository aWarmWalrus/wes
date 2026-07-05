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
