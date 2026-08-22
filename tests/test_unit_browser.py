"""Unit tests for the held-open browser (wes_browser, #039).

The speed is not the risky part -- the RESTARTS are. A page held for two hours
can be navigated away, bounced to a login wall, have its React tree detached,
or be closed under us, and every one of those is ALIVE and useless. Handing a
caller a subtly broken page produces an action that lands on nothing and
reports success, which is the failure this codebase keeps rediscovering.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "pc"))

import wes_browser  # noqa: E402


class FakePage:
    def __init__(self, url="https://sleeper.com/draft/nfl/D", ok=True):
        self._url, self.ok, self.closed = url, ok, False
        self.goto_count = 0
        self.raises = None

    @property
    def url(self):
        return self._url

    def is_closed(self):
        return self.closed

    def evaluate(self, _js):
        if self.raises:
            raise self.raises
        return self.ok

    def set_viewport_size(self, _s):
        pass

    def goto(self, url, **k):
        self.goto_count += 1
        self._url = url

    def wait_for_timeout(self, _ms):
        pass


class FakeSession:
    """Stands in for _Session. Each instance yields one page."""
    made = []

    def __init__(self):
        self.page = FakePage()
        self.exited = False
        FakeSession.made.append(self)

    def __enter__(self):
        return self.page

    def __exit__(self, *a):
        self.exited = True
        return False


def _br(monkeypatch, now=None):
    monkeypatch.setattr(wes_browser.wes_sleeper, "authenticate", lambda p: True)
    FakeSession.made = []
    return wes_browser.Browser("D", _session_cls=FakeSession,
                               _now=now or (lambda: 1000.0))


class TestReuse:
    def test_the_second_call_reuses_the_same_page(self, monkeypatch):
        br = _br(monkeypatch)
        assert br.page() is br.page()
        assert br.rebuilds == 1, "should not have relaunched"

    def test_it_parks_on_the_draft_room(self, monkeypatch):
        br = _br(monkeypatch)
        assert "draft/nfl/D" in br.page().url


class TestHealth:
    def test_a_closed_page_is_replaced(self, monkeypatch):
        br = _br(monkeypatch)
        first = br.page()
        first.closed = True
        assert br.page() is not first
        assert br.rebuilds == 2 and br.failures == 1

    def test_a_page_navigated_elsewhere_is_replaced(self, monkeypatch):
        """ALIVE and useless -- the case a liveness check would miss."""
        br = _br(monkeypatch)
        first = br.page()
        first._url = "https://sleeper.com/leagues"
        assert br.page() is not first

    def test_a_login_wall_is_replaced(self, monkeypatch):
        br = _br(monkeypatch)
        first = br.page()
        first._url = "https://sleeper.com/?redirect=/draft/nfl/D&login="
        assert br.page() is not first

    def test_a_detached_js_context_is_replaced(self, monkeypatch):
        """evaluate() raising means the page is a shell."""
        br = _br(monkeypatch)
        first = br.page()
        first.raises = RuntimeError("Execution context was destroyed")
        assert br.page() is not first

    def test_a_draft_room_that_unmounted_is_replaced(self, monkeypatch):
        br = _br(monkeypatch)
        first = br.page()
        first.ok = False           # no .draft-board anywhere
        assert br.page() is not first

    def test_healthy_is_false_before_anything_is_built(self, monkeypatch):
        br = _br(monkeypatch)
        assert br.healthy() is False


class TestRecycling:
    def test_it_recycles_after_MAX_USES(self, monkeypatch):
        br = _br(monkeypatch)
        for _ in range(wes_browser.MAX_USES + 1):
            br.page()
        assert br.rebuilds == 2
        assert br.failures == 0, "a scheduled recycle is not a failure"

    def test_it_recycles_after_MAX_AGE(self, monkeypatch):
        t = [1000.0]
        br = _br(monkeypatch, now=lambda: t[0])
        br.page()
        t[0] += wes_browser.MAX_AGE_S + 1
        br.page()
        assert br.rebuilds == 2 and br.failures == 0

    def test_the_old_session_is_closed_on_rebuild(self, monkeypatch):
        """Otherwise the profile lock leaks and the next launch cannot start."""
        br = _br(monkeypatch)
        first = br.page()
        first.closed = True
        br.page()
        assert FakeSession.made[0].exited is True


class TestFailureHandling:
    def test_a_failed_build_does_not_leave_a_session_holding_the_lock(
            self, monkeypatch):
        monkeypatch.setattr(wes_browser.wes_sleeper, "authenticate",
                            lambda p: False)
        FakeSession.made = []
        br = wes_browser.Browser("D", _session_cls=FakeSession,
                                 _now=lambda: 1000.0)
        try:
            br.page()
            assert False, "should have raised"
        except RuntimeError:
            pass
        assert FakeSession.made[0].exited is True

    def test_close_is_safe_when_nothing_was_ever_built(self, monkeypatch):
        _br(monkeypatch).close()

    def test_close_is_idempotent(self, monkeypatch):
        br = _br(monkeypatch)
        br.page()
        br.close()
        br.close()

    def test_stats_report_what_it_cost(self, monkeypatch):
        br = _br(monkeypatch)
        br.page()
        st = br.stats()
        assert st["rebuilds"] == 1 and st["failures"] == 0


class TestOneSessionAtATime:
    """A held Browser plus a code path that opens its own session is two
    Playwright instances in one thread. Playwright's own error --  "Sync API
    inside the asyncio loop" -- says nothing about the real mistake, and the
    real mistake forfeited a pick in a full mock (2026-08-21)."""

    def test_a_second_session_is_refused_with_a_useful_message(self,
                                                               monkeypatch):
        import wes_sleeper
        monkeypatch.setattr(wes_sleeper._Session, "_live", 1)
        try:
            wes_sleeper._Session().__enter__()
            assert False, "should have refused"
        except RuntimeError as e:
            assert "already open" in str(e)
            assert "held" in str(e), "must point at the fix, not just the fault"

    def test_the_counter_is_released_even_if_closing_throws(self, monkeypatch):
        """A leaked counter would lock out every later session -- worse than
        the bug it guards against."""
        import wes_sleeper

        class Boom:
            def close(self):
                raise RuntimeError("close failed")

        monkeypatch.setattr(wes_sleeper._Session, "_live", 1)
        sess = wes_sleeper._Session()
        sess._ctx = Boom()
        sess._pw = None
        try:
            sess.__exit__(None, None, None)
        except RuntimeError:
            pass
        assert wes_sleeper._Session._live == 0
