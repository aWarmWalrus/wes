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
        # A PENDING alert, which must be dropped. This was `PiHot` on the
        # Pi's node_exporter until that host was retired (2026-09-02); the rule
        # is gone but the behaviour under test never depended on which rule it
        # was, so it is repointed rather than deleted.
        {"labels": {"alertname": "EDiskLow", "instance": "10.0.0.168:9182",
                    "job": "pc_windows"},
         "annotations": {"summary": "not firing yet"},
         "state": "pending", "value": "0.08"},
    ]}}

    def test_parse_keys_by_rule_and_instance(self):
        alerts = wd.parse_alerts(self.PAYLOAD)
        assert len(alerts) == 3  # same rule on two targets = two; pending dropped
        gpu = alerts["TargetDown|10.0.0.168:9835"]
        assert gpu["alertname"] == "TargetDown" and gpu["job"] == "pc_gpu"
        assert "missing for 5 minutes" in gpu["summary"]

    def test_parse_ignores_pending(self):
        assert "EDiskLow|10.0.0.168:9182" not in wd.parse_alerts(self.PAYLOAD)

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


class TestFantasyWatcher:
    """The Yahoo-write notifier (#029): watches wes_execute's ledger file for a
    real write, phrases it via Jarvis, and DMs the owner — the "content exists,
    doesn't get pushed" gap the owner asked to close."""

    def _entry(self, executed=True, reason="", ts=100.0):
        return {
            "ts": ts, "team_key": "nfl.l.957011.t.4", "name": "Charles's Pop",
            "sport": "nfl", "action_type": "set_lineup", "autonomy": "auto",
            "moves": [{"player_key": "1", "name": "Cam Skattebo",
                      "from_slot": "BN", "to_slot": "RB"},
                     {"player_key": "2", "name": "Breece Hall",
                      "from_slot": "RB", "to_slot": "BN"}],
            "why": ["Started Cam Skattebo (14.46 pts) at RB over "
                   "Breece Hall (11.85 pts)."],
            "allowed": True, "reason": reason, "executed": executed,
            "dry_run": False,
        }

    def test_fetch_only_returns_real_or_partial_writes(self, tmp_path, monkeypatch):
        path = tmp_path / "ledger.jsonl"
        lines = [
            self._entry(executed=False, ts=1.0),   # routine no-op — excluded
            self._entry(executed=True, ts=2.0),     # real write — included
            self._entry(executed="unknown", ts=3.0),  # partial — included
        ]
        path.write_text("\n".join(json.dumps(l) for l in lines), encoding="utf-8")
        monkeypatch.setattr(wd.wes_execute, "LEDGER_FILE", str(path))
        out = wd.fetch_new_fantasy_events(0.0)
        assert [e["ts"] for e in out] == [2.0, 3.0]

    def test_fetch_respects_since_ts(self, tmp_path, monkeypatch):
        path = tmp_path / "ledger.jsonl"
        path.write_text(json.dumps(self._entry(ts=5.0)), encoding="utf-8")
        monkeypatch.setattr(wd.wes_execute, "LEDGER_FILE", str(path))
        assert wd.fetch_new_fantasy_events(10.0) == []
        assert len(wd.fetch_new_fantasy_events(1.0)) == 1

    def test_fetch_missing_ledger_is_empty_not_an_error(self, tmp_path, monkeypatch):
        monkeypatch.setattr(wd.wes_execute, "LEDGER_FILE",
                            str(tmp_path / "nope.jsonl"))
        assert wd.fetch_new_fantasy_events(0.0) == []

    def test_fetch_tolerates_a_corrupt_line(self, tmp_path, monkeypatch):
        path = tmp_path / "ledger.jsonl"
        path.write_text("not json\n" + json.dumps(self._entry(ts=2.0)),
                        encoding="utf-8")
        monkeypatch.setattr(wd.wes_execute, "LEDGER_FILE", str(path))
        out = wd.fetch_new_fantasy_events(0.0)
        assert len(out) == 1

    def test_describe_real_write_reports_not_proposes(self):
        """This already happened — the phrasing instruction must say so, not
        ask for approval (that would be a lie about a completed action)."""
        desc = wd.describe_fantasy_event(self._entry())
        assert "already happened" in desc or "you're reporting it" in desc
        assert "Cam Skattebo" in desc and "Breece Hall" in desc
        assert "14.46" in desc  # the real WHY, not re-derived or invented

    def test_describe_partial_failure_is_cautious_not_definitive(self):
        entry = self._entry(executed="unknown", reason="mid-plan error: X")
        desc = wd.describe_fantasy_event(entry)
        assert "inconsistent" in desc.lower() or "may be" in desc.lower()
        assert "do not imply" in desc.lower()

    def test_raw_summary_fallback_names_the_moves(self):
        text = wd.raw_fantasy_summary(self._entry())
        assert "Cam Skattebo" in text and "🏈" in text

    def test_raw_summary_partial_failure_uses_warning_tone(self):
        text = wd.raw_fantasy_summary(self._entry(executed="unknown"))
        assert "⚠️" in text and "check it directly" in text.lower()

    def test_explain_fantasy_event_posts_announce(self, monkeypatch):
        seen = {}

        def fake_post(path, body, timeout=120):
            seen["path"], seen["body"] = path, body
            return {"reply": "Set Skattebo over Hall for the RB spot."}
        monkeypatch.setattr(wd, "_post_json", fake_post)
        out = wd.explain_fantasy_event(self._entry())
        assert seen["path"] == "/announce"
        assert seen["body"]["channel"] == wd.CONV_CHANNEL
        assert out == "Set Skattebo over Hall for the RB spot."

    def test_watcher_dms_a_real_write_then_falls_back_on_server_down(
            self, tmp_path, monkeypatch):
        import asyncio
        import time as _time

        path = tmp_path / "ledger.jsonl"
        # Must be AFTER `seen_ts` (real time.time() captured when the watcher
        # starts) to survive the filter — a hardcoded "large-looking" constant
        # like 999999999 is actually BEFORE the real current epoch (~1.78e9).
        path.write_text(json.dumps(self._entry(ts=_time.time() + 1000)),
                        encoding="utf-8")
        monkeypatch.setattr(wd.wes_execute, "LEDGER_FILE", str(path))
        monkeypatch.setattr(wd, "FANTASY_POLL_S", 0.001)

        def boom(entry):
            raise OSError("server down")
        monkeypatch.setattr(wd, "explain_fantasy_event", boom)

        sent = []
        n = {"i": 0}

        class FakeUser:
            async def send(self, t):
                sent.append(t)

        class FakeClient:
            async def wait_until_ready(self):
                pass

            def is_closed(self):
                n["i"] += 1
                return n["i"] > 2

            async def fetch_user(self, uid):
                return FakeUser()

        asyncio.run(wd.fantasy_watch(FakeClient()))
        assert any("🏈" in m and "Cam Skattebo" in m for m in sent)

    def test_watcher_does_not_replay_old_history_on_startup(
            self, tmp_path, monkeypatch):
        """seen_ts is seeded to 'now' — an entry from before the bot started
        must not be DMed on the first poll."""
        import asyncio

        path = tmp_path / "ledger.jsonl"
        path.write_text(json.dumps(self._entry(ts=1.0)), encoding="utf-8")
        monkeypatch.setattr(wd.wes_execute, "LEDGER_FILE", str(path))
        monkeypatch.setattr(wd, "FANTASY_POLL_S", 0.001)

        sent = []
        n = {"i": 0}

        class FakeUser:
            async def send(self, t):
                sent.append(t)

        class FakeClient:
            async def wait_until_ready(self):
                pass

            def is_closed(self):
                n["i"] += 1
                return n["i"] > 2

            async def fetch_user(self, uid):
                return FakeUser()

        asyncio.run(wd.fantasy_watch(FakeClient()))
        assert sent == []

    def test_watcher_survives_a_poll_exception(self, monkeypatch):
        """A crash reading the ledger must not kill the watcher — same
        must-never-die rule as alert_watch."""
        import asyncio

        def boom(since_ts):
            raise OSError("disk error")
        monkeypatch.setattr(wd, "fetch_new_fantasy_events", boom)
        monkeypatch.setattr(wd, "FANTASY_POLL_S", 0.001)

        n = {"i": 0}

        class FakeClient:
            async def wait_until_ready(self):
                pass

            def is_closed(self):
                n["i"] += 1
                return n["i"] > 3

        asyncio.run(wd.fantasy_watch(FakeClient()))   # must not raise


class TestRecommendationNotifications:
    """#035 — the scheduled run SUGGESTS roster moves. It fires daily, so the
    same standing suggestion must not nag every morning: notify on CHANGE, the
    way alert_watch notifies on state change rather than every poll."""

    def _rec(self, ts, drop="Addison", add="Washington", team="nfl.l.1.t.4"):
        return {"ts": ts, "team_key": team, "name": "Test", "sport": "nfl",
                "action_type": "add_drop", "autonomy": "auto",
                "moves": [{"drop": drop, "add": add, "gain": 2.7}],
                "why": [f"Drop {drop} for {add}."],
                "executed": False, "dry_run": True}

    def _write(self, tmp_path, monkeypatch, entries):
        p = tmp_path / "ledger.jsonl"
        p.write_text("\n".join(json.dumps(e) for e in entries), encoding="utf-8")
        monkeypatch.setattr(wd.wes_execute, "LEDGER_FILE", str(p))
        return p

    def test_a_new_recommendation_is_notified(self, tmp_path, monkeypatch):
        self._write(tmp_path, monkeypatch, [self._rec(100)])
        out = wd.fetch_new_fantasy_events(0)
        assert len(out) == 1 and out[0]["action_type"] == "add_drop"

    def test_fetch_returns_every_new_recommendation(self, tmp_path, monkeypatch):
        """fetch answers "what is NEW by time" only — deciding whether a
        repeat is worth saying is the watcher's job (see the dedup tests
        below), so the same suggestion logged three times is three rows here."""
        self._write(tmp_path, monkeypatch,
                    [self._rec(100), self._rec(200), self._rec(300)])
        assert len(wd.fetch_new_fantasy_events(0)) == 3

    def test_fetch_does_not_replay_history_before_since_ts(
            self, tmp_path, monkeypatch):
        self._write(tmp_path, monkeypatch, [self._rec(100), self._rec(500)])
        assert [e["ts"] for e in wd.fetch_new_fantasy_events(300)] == [500]

    def test_a_no_op_run_is_never_notified(self, tmp_path, monkeypatch):
        """"already optimal" writes a ledger row with no moves — silence."""
        empty = dict(self._rec(100), moves=[], why=[])
        self._write(tmp_path, monkeypatch, [empty])
        assert wd.fetch_new_fantasy_events(0) == []

    def test_real_writes_are_still_notified_alongside(self, tmp_path, monkeypatch):
        write = {"ts": 400, "team_key": "nfl.l.1.t.4", "name": "Test",
                 "action_type": "set_lineup", "moves": [{"name": "X"}],
                 "executed": True, "dry_run": False}
        self._write(tmp_path, monkeypatch, [self._rec(100), write])
        out = wd.fetch_new_fantasy_events(0)
        assert len(out) == 2

    def test_recommendation_framing_says_nothing_was_done(self):
        desc = wd.describe_fantasy_event(self._rec(100))
        assert "NOTHING HAS BEEN DONE" in desc
        assert "suggestion" in desc.lower()
        assert "permanent" in desc.lower()

    def test_recommendation_fallback_dm_is_clearly_a_suggestion(self):
        text = wd.raw_fantasy_summary(self._rec(100))
        assert "suggestion" in text.lower()
        assert "nothing was changed" in text.lower()

class TestRecommendationDedup:
    """The daily-nag guard, at the layer that actually owns it. The scheduled
    run fires every morning, so an unchanged standing suggestion must be DMed
    ONCE, not forever."""

    def _rec(self, ts, drop="Addison", add="Washington", team="t1"):
        return {"ts": ts, "team_key": team, "name": "Test",
                "action_type": "add_drop",
                "moves": [{"drop": drop, "add": add}],
                "why": [f"Drop {drop} for {add}."],
                "executed": False, "dry_run": True}

    def _run(self, monkeypatch, batches):
        """Drive fantasy_watch over successive fetch results; return DM texts.
        Each poll consumes one batch; the watcher stops when they run out."""
        import asyncio

        pending = list(batches)
        sent = []
        monkeypatch.setattr(wd, "FANTASY_POLL_S", 0.001)
        monkeypatch.setattr(
            wd, "explain_fantasy_event",
            lambda e: f"DM:{e.get('team_key')}:{wd._recommendation_signature(e)}")
        monkeypatch.setattr(
            wd, "fetch_new_fantasy_events",
            lambda since_ts: pending.pop(0) if pending else [])

        class FakeUser:
            async def send(self, t):
                sent.append(t)

        class FakeClient:
            async def wait_until_ready(self):
                pass

            def is_closed(self):
                return not pending      # stop once every batch is consumed

            async def fetch_user(self, uid):
                return FakeUser()

        asyncio.run(wd.fantasy_watch(FakeClient()))
        return sent

    def test_an_unchanged_suggestion_is_dmed_once(self, monkeypatch):
        sent = self._run(monkeypatch, [[self._rec(100)], [self._rec(200)],
                                       [self._rec(300)]])
        assert len(sent) == 1

    def test_a_changed_suggestion_is_dmed_again(self, monkeypatch):
        sent = self._run(monkeypatch,
                         [[self._rec(100)],
                          [self._rec(200, drop="Ferguson", add="Diggs")]])
        assert len(sent) == 2

    def test_a_reverted_suggestion_is_dmed_again(self, monkeypatch):
        """A -> B -> A is a real change each time; the middle one isn't noise."""
        sent = self._run(monkeypatch,
                         [[self._rec(100)],
                          [self._rec(200, drop="X", add="Y")],
                          [self._rec(300)]])
        assert len(sent) == 3

    def test_dedup_is_per_team(self, monkeypatch):
        sent = self._run(monkeypatch,
                         [[self._rec(100, team="t1"),
                           self._rec(101, team="t2")]])
        assert len(sent) == 2

    def test_real_writes_are_never_deduped(self, monkeypatch):
        """A repeated WRITE is a repeated real-world event, not a nag."""
        write = {"ts": 100, "team_key": "t1", "name": "T",
                 "action_type": "set_lineup", "moves": [{"name": "X"}],
                 "executed": True}
        sent = self._run(monkeypatch, [[write], [dict(write, ts=200)]])
        assert len(sent) == 2


class TestCertifiSslContext:
    """certifi_default_ssl_context — the pre-`import discord` TLS shim.

    On the PC's conda env (OpenSSL 3.5.7) the stock default-context factory
    dies reading the Windows store, and aiohttp calls it at import time; the
    shim reroutes no-CA-source calls to certifi. These tests pin the wrapper's
    contract without touching the real store: explicit CA sources and non-
    server purposes must pass through untouched, and the patch must restore
    cleanly (the tests may run on machines where the default path works)."""

    def _patched_ssl(self):
        import ssl
        orig = ssl.create_default_context
        wd.certifi_default_ssl_context()
        return ssl, orig

    def test_bare_call_gets_certifi_cas(self):
        import certifi
        ssl, orig = self._patched_ssl()
        try:
            ctx = ssl.create_default_context()
            # certifi's bundle alone is ~150 roots; a context that silently
            # loaded nothing (the failure this guards against) would have 0.
            assert len(ctx.get_ca_certs()) > 50
        finally:
            ssl.create_default_context = orig

    def test_explicit_cafile_wins(self, tmp_path):
        import certifi
        ssl, orig = self._patched_ssl()
        try:
            one_cert = tmp_path / "one.pem"
            pem = open(certifi.where(), encoding="utf-8").read()
            end = pem.index("-----END CERTIFICATE-----") + len(
                "-----END CERTIFICATE-----")
            start = pem.index("-----BEGIN CERTIFICATE-----")
            one_cert.write_text(pem[start:end] + "\n", encoding="utf-8")
            ctx = ssl.create_default_context(cafile=str(one_cert))
            assert len(ctx.get_ca_certs()) == 1  # ours, not certifi's bundle
        finally:
            ssl.create_default_context = orig

    def test_client_auth_purpose_untouched(self, monkeypatch):
        # CLIENT_AUTH contexts (a server verifying clients) must not be
        # silently pointed at certifi's server-auth roots.
        ssl, orig = self._patched_ssl()
        try:
            seen = {}

            def spy(purpose=ssl.Purpose.SERVER_AUTH, *, cafile=None,
                    capath=None, cadata=None):
                seen.update(purpose=purpose, cafile=cafile)
                return ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)

            # The wrapper closed over the factory it replaced; spy one level
            # deeper by re-patching and re-wrapping.
            ssl.create_default_context = spy
            wd.certifi_default_ssl_context()
            ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
            assert seen["purpose"] == ssl.Purpose.CLIENT_AUTH
            assert seen["cafile"] is None
        finally:
            ssl.create_default_context = orig
