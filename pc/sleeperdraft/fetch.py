"""The one outbound HTTP call, with a small per-URL cache.

Sleeper's read API is public and unauthenticated, but it is also the thing a
draft loop hammers -- picks and status get polled every few seconds -- and its
docs ask callers not to pull the 14MB player dump more than once a day. So
every read goes through here and every caller states a TTL.

DELEGATES when a host application already owns an HTTP layer. Inside WES that
is `wes_http`, which has retries, a shared cache and a single User-Agent; using
it keeps one cache and one set of manners rather than two that drift. Standalone
the fallback below does the same job with no dependencies, so the package
installs with nothing but Playwright.
"""
import json
import threading
import time
import urllib.request

UA = "Mozilla/5.0 (sleeperdraft; +https://github.com/aWarmWalrus/sleeperdraft)"
DEFAULT_TTL = 20.0
DEFAULT_TIMEOUT = 15.0

try:                                  # host-provided client, if there is one
    import wes_http as _host
except ImportError:                   # pragma: no cover - standalone install
    _host = None

_cache = {}
_lock = threading.Lock()


def _fallback_get_json(url, ttl=DEFAULT_TTL, timeout=DEFAULT_TIMEOUT):
    """Cached JSON GET. Raises on transport or decode failure -- the layer
    above owns how a failure is described to a person."""
    now = time.time()
    with _lock:
        hit = _cache.get(url)
        if hit and hit[0] > now:
            return hit[1]
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        value = json.loads(resp.read().decode("utf-8"))
    if ttl > 0:
        with _lock:
            _cache[url] = (now + ttl, value)
    return value


def get_json(url, ttl=DEFAULT_TTL, timeout=DEFAULT_TIMEOUT):
    if _host is not None:
        return _host.get_json(url, ttl=ttl, timeout=timeout)
    return _fallback_get_json(url, ttl=ttl, timeout=timeout)


def clear_cache():
    if _host is not None:
        return _host.clear_cache()
    with _lock:
        _cache.clear()
