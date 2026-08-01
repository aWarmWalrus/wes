"""Unit tests for the shared raw data layer (wes_http, #034 layer 1).

No network: every test injects `_fetch_fn`, and cache expiry is driven by an
injected `_now` rather than by sleeping.
"""
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

        def fake_urlopen(req, timeout=0):
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

        def fake_urlopen(req, timeout=0):
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

        def fake_urlopen(req, timeout=0):
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
