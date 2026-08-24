"""Raw data layer — the ONE outbound HTTP client (ticket #034, layer 1).

Architecture: `docs/data-architecture.md`. This module is the bottom layer.

  KNOWS:     HTTP, caching, retries, timeouts, User-Agents, politeness.
  MUST NOT:  know what a fantasy point is, what a roster is, or which sport.

It replaces four hand-rolled fetchers that had drifted apart — `wes_nba._get`
(JSON, 12s, 20s cache), `wes_nba._get_text` (text, 12s, 300s cache),
`wes_nfl._get_json` (15s, NO cache) and `wes_draft._get_json` (15s, NO cache) —
three User-Agent strings and two timeouts between them. The missing caching was
not cosmetic: the NFL pool makes FOUR ESPN calls and sat uncached inside the
~30s "who should I start" turn, re-fetching an entire season of stats every time.

DESIGN NOTES

- **Caching is per-URL with a per-call TTL**, because "the live score right now"
  and "last season's stat totals" want wildly different freshness. Callers pick;
  the default is deliberately short.
- **Failures raise here** and are caught by the layer above, which owns the
  degradation wording. Layer 1 has no vocabulary for talking to a user.
- **`_now` is injectable** so cache-expiry logic is testable without sleeping.
- No global rate limiter yet. One client is the place to add one when a scraper
  canary (#031) or ESPN politeness needs it — that is the point of funnelling
  every fetch through here.
"""
import json
import threading
import time
import urllib.error
import urllib.request

# A plain, honest identifier. Previously three different strings claimed to be
# three different products; this is one home-automation client.
UA = "Mozilla/5.0 (WES home assistant; +https://github.com/aWarmWalrus/wes)"
# Reddit 403s a bare/bot UA. Its RSS (unlike the JSON API) serves fine to a
# browser-like UA with no auth — JSON is blocked and OAuth needs an app, so RSS
# is the reliable no-key path (#027 P1b). Kept verbatim: it is load-bearing.
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
              "AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/120.0 Safari/537.36")

DEFAULT_TIMEOUT = 12.0
# Short by default: a "score right now" repeated within a turn should be cheap,
# but nothing should go stale without asking.
DEFAULT_TTL = 20.0
# Long-lived data (a completed season's stats, league settings) — the caller
# opts in explicitly rather than this being a magic global.
SEASON_TTL = 900.0
# Reddit rate-limits RSS aggressively (429 on rapid repeats) and fan discussion
# moves slowly.
TEXT_TTL = 300.0

_cache = {}          # (url, kind) -> (expiry_ts, value)
_lock = threading.Lock()   # the Flask server serves turns from several threads


def clear_cache():
    """Drop everything. For tests and for a forced refresh after a drift alert."""
    with _lock:
        _cache.clear()


def cache_size():
    with _lock:
        return len(_cache)


def _cached(key, max_age, produce, now, cacheable=None):
    """Return a cached value or produce one.

    `max_age` is how old a stored copy THIS caller will accept, in seconds; 0
    always produces. The cache holds WHEN a value was fetched rather than when
    it expires, so two callers wanting different freshness of the same key
    cannot decide it for one another.

    THAT ORDERING WAS A REAL BUG, and a silent one. The read used to compare
    against an expiry chosen by whoever fetched first and never consulted the
    current caller, so `ttl=0` meant "do not STORE this response" while still
    happily RETURNING a stored one. Every "must be current" read in
    `wes_sleeper` passes 0 — the availability check immediately before a pick,
    the pre-start draft status, and the post-write pick verification — and all
    three could be answered from a copy of the world taken before the write
    they were checking. Verifying against a cache is not verifying.

    `cacheable(value) -> bool` lets the CALLER veto storing a technically-valid
    but useless response. This exists because of a real bug (#035, 2026-07-31):
    ESPN sometimes answers a paginated request with HTTP 200 and no `athletes`
    key at all. That parsed fine, so it was cached for the full 900s season TTL
    — which silently made the per-page retry above it DEAD CODE, since every
    retry re-read the same cached empty body instead of asking ESPN again. The
    visible symptom was a roster recommendation that appeared and vanished
    between runs, and an owner-approved move that turned into a no-op."""
    if max_age > 0:
        hit = _cache.get(key)
        if hit and (now - hit[0]) <= max_age:
            return hit[1]
    value = produce()
    # STORED even when max_age is 0: a response fetched just now is the freshest
    # thing available, and the caller that forced it will not reuse it anyway.
    if cacheable is None or cacheable(value):
        with _lock:
            _cache[key] = (now, value)
    return value


def _fetch(url, headers, timeout, retries):
    """One request, with retries on transient failures only.

    A 404 or a malformed URL will fail identically on retry, so retrying it just
    makes the caller wait longer before hearing the same bad news. Retry the
    things that are actually worth retrying: timeouts, connection resets, and
    5xx / 429 from the far end."""
    attempt, delay = 0, 0.4
    while True:
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            transient = e.code in (429, 500, 502, 503, 504)
            if not transient or attempt >= retries:
                raise
        except (urllib.error.URLError, TimeoutError, OSError):
            if attempt >= retries:
                raise
        attempt += 1
        time.sleep(delay)
        delay *= 2


def get_json(url, ttl=DEFAULT_TTL, timeout=DEFAULT_TIMEOUT, retries=1,
             ua=UA, _now=None, _fetch_fn=None, cacheable=None, headers=None,
             max_age=None):
    """Fetch and parse JSON, cached per URL. Raises on failure — the layer above
    owns how that becomes a sentence.

    `ttl` and `max_age` are the same number under two names: how old a stored
    copy may be before this call refuses it. `max_age` exists because
    `sleeperdraft` is routed through this client via `fetch.use_host` and calls
    it with that keyword — it is the package's name for the idea, and accepting
    both is cheaper than a shim that would have to be kept in step. When both
    are given, `max_age` wins.

    `cacheable(payload) -> bool` optionally vetoes caching a response the caller
    considers useless (e.g. a 200 with no data). Without it, a transient empty
    answer is remembered for the whole TTL and every retry re-reads it."""
    ttl = ttl if max_age is None else max_age
    now = _now if _now is not None else time.time()
    fetch = _fetch_fn or _fetch

    hdrs = {"User-Agent": ua}
    if headers:
        hdrs.update(headers)

    def produce():
        return json.loads(fetch(url, hdrs, timeout, retries).decode())
    # The cache key includes the extra headers. ESPN's kona endpoint varies its
    # RESPONSE by an `x-fantasy-filter` header on an otherwise identical URL, so
    # keying on the URL alone would serve one filter's answer for another's
    # question — a cache hit that is silently the wrong data.
    key = (url, "json", tuple(sorted((headers or {}).items())))
    return _cached(key, ttl, produce, now, cacheable)


def get_text(url, ttl=TEXT_TTL, timeout=DEFAULT_TIMEOUT, retries=1,
             ua=BROWSER_UA, _now=None, _fetch_fn=None):
    """Fetch text (RSS/HTML), cached per URL. Defaults to the browser UA because
    the only text sources so far are ones that reject a bot UA."""
    now = _now if _now is not None else time.time()
    fetch = _fetch_fn or _fetch

    def produce():
        return fetch(url, {"User-Agent": ua}, timeout, retries).decode(
            "utf-8", "replace")
    return _cached((url, "text"), ttl, produce, now)
