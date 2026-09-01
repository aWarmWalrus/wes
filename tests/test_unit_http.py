"""Unit tests for the shared raw data layer (wes_http, #034 layer 1).

No network: every test injects `_fetch_fn`, and cache expiry is driven by an
injected `_now` rather than by sleeping.
"""
import builtins
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "pc"))

import wes_http  # noqa: E402

# TestRetry patches urlopen, so the Request must be constructible: a real scheme.
URL = "http://test.invalid/x"


@pytest.fixture(autouse=True)
def _clean():
    wes_http.clear_cache()
    yield
    wes_http.clear_cache()


class TestCaching:
    def test_second_call_within_ttl_does_not_refetch(self):
        calls = []

        def fake(url, headers, timeout, retries):
            calls.append(url)
            return b'{"v": 1}'
        for _ in range(3):
            assert wes_http.get_json("u", _fetch_fn=fake, _now=100.0) == {"v": 1}
        assert len(calls) == 1

    def test_call_after_ttl_refetches(self):
        calls = []

        def fake(url, headers, timeout, retries):
            calls.append(url)
            return b'{"v": 1}'
        wes_http.get_json("u", ttl=10, _fetch_fn=fake, _now=100.0)
        wes_http.get_json("u", ttl=10, _fetch_fn=fake, _now=109.0)   # still warm
        assert len(calls) == 1
        wes_http.get_json("u", ttl=10, _fetch_fn=fake, _now=111.0)   # expired
        assert len(calls) == 2

    def test_distinct_urls_cache_separately(self):
        calls = []

        def fake(url, headers, timeout, retries):
            calls.append(url)
            return b'{"v": 1}'
        wes_http.get_json("a", _fetch_fn=fake, _now=1.0)
        wes_http.get_json("b", _fetch_fn=fake, _now=1.0)
        assert calls == ["a", "b"]

    def test_json_and_text_do_not_share_a_cache_entry(self):
        """Same URL, different parse — one must not poison the other."""
        def fake(url, headers, timeout, retries):
            return b'{"v": 1}'
        assert wes_http.get_json("same", _fetch_fn=fake, _now=1.0) == {"v": 1}
        assert wes_http.get_text("same", _fetch_fn=fake, _now=1.0) == '{"v": 1}'

    def test_ttl_zero_disables_caching(self):
        calls = []

        def fake(url, headers, timeout, retries):
            calls.append(url)
            return b'{"v": 1}'
        wes_http.get_json("u", ttl=0, _fetch_fn=fake, _now=1.0)
        wes_http.get_json("u", ttl=0, _fetch_fn=fake, _now=1.0)
        assert len(calls) == 2

    def test_ttl_zero_ignores_a_WARM_entry(self):
        """The case the test above does not cover, and the bug that hid there.

        `test_ttl_zero_disables_caching` only ever calls with ttl=0, so no entry
        is ever written and there is nothing to be wrongly served — it passed
        before this was fixed and after. The real question is what ttl=0 does
        when SOMEONE ELSE has warmed the URL, and the answer used to be "return
        their copy": the read compared against an expiry chosen by whoever
        fetched first and never consulted the current caller, so ttl=0 meant
        "do not STORE" and never "do not USE".

        Every must-be-current read in wes_sleeper passes 0 — the availability
        check immediately before a pick, the pre-start draft status, and the
        post-write pick verification. All three could be answered from a copy of
        the world taken before the write they were checking."""
        calls = []

        def fake(url, headers, timeout, retries):
            calls.append(url)
            return b'{"v": 1}'
        wes_http.get_json("u", ttl=900, _fetch_fn=fake, _now=100.0)   # warm it
        assert len(calls) == 1
        wes_http.get_json("u", ttl=0, _fetch_fn=fake, _now=100.0)     # must refetch
        assert len(calls) == 2, "ttl=0 was served from another caller's cache"

    def test_a_forced_read_is_still_stored_for_others(self):
        """A response fetched just now is the freshest thing available, so
        keeping it can only help the next caller — and the caller that forced it
        will not reuse it anyway."""
        calls = []

        def fake(url, headers, timeout, retries):
            calls.append(url)
            return b'{"v": 1}'
        wes_http.get_json("u", ttl=0, _fetch_fn=fake, _now=100.0)
        wes_http.get_json("u", ttl=900, _fetch_fn=fake, _now=100.0)
        assert len(calls) == 1

    def test_freshness_is_judged_by_age_not_by_the_writer(self):
        """Two callers wanting different freshness of one URL must not decide
        it for each other."""
        calls = []

        def fake(url, headers, timeout, retries):
            calls.append(url)
            return b'{"v": 1}'
        wes_http.get_json("u", ttl=3, _fetch_fn=fake, _now=100.0)
        # 10s later: a caller tolerating 15s accepts it, one tolerating 3s does not
        wes_http.get_json("u", ttl=15, _fetch_fn=fake, _now=110.0)
        assert len(calls) == 1
        wes_http.get_json("u", ttl=3, _fetch_fn=fake, _now=110.0)
        assert len(calls) == 2

    def test_max_age_is_accepted_as_an_alias_for_ttl(self):
        """`sleeperdraft` is routed through this client via fetch.use_host and
        calls it with max_age=, which is its name for the same idea. A
        TypeError here means every Sleeper read fails at once."""
        calls = []

        def fake(url, headers, timeout, retries):
            calls.append(url)
            return b'{"v": 1}'
        wes_http.get_json("u", max_age=900, _fetch_fn=fake, _now=100.0)
        wes_http.get_json("u", max_age=900, _fetch_fn=fake, _now=100.0)
        assert len(calls) == 1
        wes_http.get_json("u", max_age=0, _fetch_fn=fake, _now=100.0)
        assert len(calls) == 2

    def test_clear_cache_forces_a_refetch(self):
        calls = []

        def fake(url, headers, timeout, retries):
            calls.append(url)
            return b'{"v": 1}'
        wes_http.get_json("u", _fetch_fn=fake, _now=1.0)
        wes_http.clear_cache()
        wes_http.get_json("u", _fetch_fn=fake, _now=1.0)
        assert len(calls) == 2
        assert wes_http.cache_size() == 1

    def test_season_ttl_is_much_longer_than_the_default(self):
        """Season totals can't change; live scores can. The whole point of a
        per-call TTL is that those two don't share a freshness policy."""
        assert wes_http.SEASON_TTL > wes_http.DEFAULT_TTL * 10


class TestFailures:
    def test_errors_propagate_for_the_layer_above_to_word(self):
        """Layer 1 has no vocabulary for talking to a user — it raises, and the
        data/model layers own the degradation string."""
        def boom(url, headers, timeout, retries):
            raise OSError("espn down")
        with pytest.raises(OSError):
            wes_http.get_json("u", _fetch_fn=boom, _now=1.0)

    def test_a_failure_is_not_cached(self):
        calls = []

        def flaky(url, headers, timeout, retries):
            calls.append(url)
            if len(calls) == 1:
                raise OSError("transient")
            return b'{"v": 2}'
        with pytest.raises(OSError):
            wes_http.get_json("u", _fetch_fn=flaky, _now=1.0)
        assert wes_http.get_json("u", _fetch_fn=flaky, _now=1.0) == {"v": 2}


class TestRetry:
    """Retry the transient, not the doomed: a 404 fails identically on retry and
    retrying it only makes the caller wait longer for the same bad news."""

    def _urlerror(self, code):
        import urllib.error
        return urllib.error.HTTPError("u", code, "err", {}, None)

    @pytest.mark.parametrize("code", [429, 500, 502, 503, 504])
    def test_transient_http_codes_are_retried(self, code, monkeypatch):
        monkeypatch.setattr(wes_http.time, "sleep", lambda s: None)
        attempts = []

        def fake_urlopen(req, timeout=0, **kw):
            attempts.append(1)
            raise self._urlerror(code)
        monkeypatch.setattr(wes_http.urllib.request, "urlopen", fake_urlopen)
        with pytest.raises(Exception):
            wes_http.get_json(URL, retries=2, _now=1.0)
        assert len(attempts) == 3          # initial + 2 retries

    @pytest.mark.parametrize("code", [400, 403, 404])
    def test_permanent_http_codes_are_not_retried(self, code, monkeypatch):
        monkeypatch.setattr(wes_http.time, "sleep", lambda s: None)
        attempts = []

        def fake_urlopen(req, timeout=0, **kw):
            attempts.append(1)
            raise self._urlerror(code)
        monkeypatch.setattr(wes_http.urllib.request, "urlopen", fake_urlopen)
        with pytest.raises(Exception):
            wes_http.get_json(URL, retries=3, _now=1.0)
        assert len(attempts) == 1

    def test_a_retry_that_succeeds_returns_the_value(self, monkeypatch):
        monkeypatch.setattr(wes_http.time, "sleep", lambda s: None)
        state = {"n": 0}

        class _Resp:
            def read(self):
                return b'{"ok": true}'

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake_urlopen(req, timeout=0, **kw):
            state["n"] += 1
            if state["n"] == 1:
                raise TimeoutError("slow")
            return _Resp()
        monkeypatch.setattr(wes_http.urllib.request, "urlopen", fake_urlopen)
        assert wes_http.get_json(URL, retries=2, _now=1.0) == {"ok": True}


class TestUserAgents:
    def test_json_uses_the_plain_ua(self):
        seen = {}

        def fake(url, headers, timeout, retries):
            seen.update(headers)
            return b"{}"
        wes_http.get_json("u", _fetch_fn=fake, _now=1.0)
        assert seen["User-Agent"] == wes_http.UA

    def test_text_defaults_to_the_browser_ua(self):
        """The only text sources so far (reddit RSS) 403 a bot UA."""
        seen = {}

        def fake(url, headers, timeout, retries):
            seen.update(headers)
            return b"x"
        wes_http.get_text("u", _fetch_fn=fake, _now=1.0)
        assert "Chrome" in seen["User-Agent"]

    def test_ua_is_overridable(self):
        seen = {}

        def fake(url, headers, timeout, retries):
            seen.update(headers)
            return b"{}"
        wes_http.get_json("u", ua="custom/1.0", _fetch_fn=fake, _now=1.0)
        assert seen["User-Agent"] == "custom/1.0"


class TestLayering:
    """#034: layer 1 knows transport and nothing else."""

    def test_raw_layer_imports_nothing_from_higher_layers(self):
        src = open(os.path.join(os.path.dirname(__file__), "..", "pc",
                                "wes_http.py"), encoding="utf-8").read()
        for higher in ("wes_nba", "wes_nfl", "wes_yahoo", "wes_fantasy",
                       "wes_draft", "wes_server"):
            assert f"import {higher}" not in src, higher

    def test_no_module_still_hand_rolls_its_own_fetcher(self):
        """The duplication #034 exists to remove: three fetchers, three UAs, two
        timeouts, caching in one. If one comes back, this fails."""
        pc = os.path.join(os.path.dirname(__file__), "..", "pc")
        for name in ("wes_nba.py", "wes_nfl.py", "wes_draft.py"):
            src = open(os.path.join(pc, name), encoding="utf-8").read()
            assert "urllib.request.urlopen" not in src, name


class TestCacheableVeto:
    """#035 regression, 2026-07-31. ESPN sometimes answers a paginated request
    with HTTP 200 and no data. That parsed fine, so it was CACHED for the full
    900s season TTL — which silently made the per-page retry above it dead
    code, since every retry re-read the cached emptiness instead of asking
    ESPN again. Symptom: a roster recommendation that appeared and vanished
    between runs, and an owner-APPROVED move that turned into a no-op."""

    def test_an_unusable_response_is_not_cached_and_a_retry_recovers(self):
        calls = []

        def flaky(url, headers, timeout, retries):
            calls.append(url)
            if len(calls) == 1:
                return b'{"league": {}}'      # 200, but no data
            return b'{"athletes": [{"a": 1}]}'

        usable = lambda p: bool(p.get("athletes"))  # noqa: E731
        first = wes_http.get_json("u", ttl=900, _fetch_fn=flaky, _now=1.0,
                                  cacheable=usable)
        second = wes_http.get_json("u", ttl=900, _fetch_fn=flaky, _now=2.0,
                                   cacheable=usable)
        assert first == {"league": {}}          # returned, but not remembered
        assert second == {"athletes": [{"a": 1}]}
        assert len(calls) == 2                  # the retry REACHED the network

    def test_a_usable_response_is_still_cached(self):
        calls = []

        def fetch(url, headers, timeout, retries):
            calls.append(url)
            return b'{"athletes": [{"a": 1}]}'

        usable = lambda p: bool(p.get("athletes"))  # noqa: E731
        for _ in range(3):
            wes_http.get_json("u2", ttl=900, _fetch_fn=fetch, _now=1.0,
                              cacheable=usable)
        assert len(calls) == 1

    def test_without_the_veto_behaviour_is_unchanged(self):
        """Callers that don't pass `cacheable` keep the old semantics."""
        calls = []

        def fetch(url, headers, timeout, retries):
            calls.append(url)
            return b'{"league": {}}'

        wes_http.get_json("u3", ttl=900, _fetch_fn=fetch, _now=1.0)
        wes_http.get_json("u3", ttl=900, _fetch_fn=fetch, _now=2.0)
        assert len(calls) == 1


class TestNflEmptyPayloadGuard:
    def test_espn_empty_responses_are_rejected(self):
        import wes_nfl as nfl
        assert nfl._has_payload({"athletes": [{"x": 1}]}) is True
        assert nfl._has_payload({"teams": [{"x": 1}]}) is True
        assert nfl._has_payload({"league": {}}) is False
        assert nfl._has_payload({"athletes": []}) is False
        assert nfl._has_payload(None) is False


class TestSSLContext:
    """The certifi workaround for a malformed certificate in the Windows store
    (2026-08-30). It shipped with no test of its own, and the one thing it did
    do -- pass `context=` to urlopen -- broke every fake in TestRetry, which is
    how the omission surfaced. Pin both halves here."""

    def test_the_context_is_passed_to_urlopen(self, monkeypatch):
        """The whole point of the fix. Without this argument the request falls
        back to Python's default context, which enumerates the cert store and
        dies on the bad entry."""
        seen = {}

        class _Resp:
            def read(self):
                return b"{}"

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake_urlopen(req, timeout=0, **kw):
            seen.update(kw)
            return _Resp()
        monkeypatch.setattr(wes_http.urllib.request, "urlopen", fake_urlopen)
        wes_http._fetch(URL, {}, 1.0, 0)
        assert "context" in seen
        assert seen["context"] is wes_http._SSL

    def test_it_degrades_to_none_without_certifi(self, monkeypatch):
        """A machine with a healthy store must be unaffected. `None` means
        'urlopen, use your default' -- not 'skip verification'."""
        real_import = builtins.__import__

        def no_certifi(name, *a, **kw):
            if name == "certifi":
                raise ImportError("no certifi")
            return real_import(name, *a, **kw)
        monkeypatch.setattr(builtins, "__import__", no_certifi)
        assert wes_http._ssl_context() is None

    def test_it_builds_a_verifying_context_when_certifi_is_present(self):
        """The fallback must not be the normal path: a context that silently
        stopped verifying would look identical at this level."""
        import ssl as _ssl
        ctx = wes_http._ssl_context()
        if ctx is None:
            pytest.skip("certifi not installed in this environment")
        assert ctx.verify_mode == _ssl.CERT_REQUIRED
        assert ctx.check_hostname is True
