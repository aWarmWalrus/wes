"""Unit tests for the held-open browser (sleeperdraft.browser, #039).

The speed is not the risky part -- the RESTARTS are. A page held for two hours
can be navigated away, bounced to a login wall, have its React tree detached,
or be closed under us, and every one of those is ALIVE and useless. Handing a
caller a subtly broken page produces an action that lands on nothing and
reports success, which is the failure this codebase keeps rediscovering.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "pc"))

from sleeperdraft import browser as sdbrowser  # noqa: E402
from sleeperdraft import session as sdsession  # noqa: E402


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

    def evaluate(self, js, arg=None):
        if self.raises:
            raise self.raises
        # The autopick probe passes a selector; answer "no such control" so
        # these tests stay about the browser lifecycle, not the toggle.
        if arg is not None or "checkbox" in str(js):
            return None
        return self.ok

    def query_selector(self, _sel):
        return None

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
    monkeypatch.setattr(sdsession, "authenticate", lambda p: True)
    FakeSession.made = []
    return sdbrowser.Browser("D", _session_cls=FakeSession,
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
        for _ in range(sdbrowser.MAX_USES + 1):
            br.page()
        assert br.rebuilds == 2
        assert br.failures == 0, "a scheduled recycle is not a failure"

    def test_it_recycles_after_MAX_AGE(self, monkeypatch):
        t = [1000.0]
        br = _br(monkeypatch, now=lambda: t[0])
        br.page()
        t[0] += sdbrowser.MAX_AGE_S + 1
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
        monkeypatch.setattr(sdsession, "authenticate",
                            lambda p: False)
        FakeSession.made = []
        br = sdbrowser.Browser("D", _session_cls=FakeSession,
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
        monkeypatch.setattr(sdsession.Session, "_live", 1)
        try:
            sdsession.Session().__enter__()
            assert False, "should have refused"
        except RuntimeError as e:
            assert "already open" in str(e)
            assert "held" in str(e), "must point at the fix, not just the fault"

    def test_the_counter_is_released_even_if_closing_throws(self, monkeypatch):
        """A leaked counter would lock out every later session -- worse than
        the bug it guards against."""
        class Boom:
            def close(self):
                raise RuntimeError("close failed")

        monkeypatch.setattr(sdsession.Session, "_live", 1)
        sess = sdsession.Session()
        sess._ctx = Boom()
        sess._pw = None
        try:
            sess.__exit__(None, None, None)
        except RuntimeError:
            pass
        assert sdsession.Session._live == 0


class TestAutopickIsClearedOnBuild:
    """Sleeper turns AUTO-PICK on by itself after one missed pick and leaves
    it on, so one miss costs the REST of the draft rather than one pick.

    A fresh session is the first chance to notice, and the lifecycle tests
    above deliberately stub the toggle away to stay about the lifecycle -- so
    without these, the clear-on-build path had no test at all.
    """

    class TogglePage(FakePage):
        """A page whose AUTO-PICK control is present and ON."""

        def __init__(self, on=True):
            super().__init__()
            self.state = {"on": on}
            self.slider_clicks = 0
            page = self

            class Slider:
                def click(_s):
                    page.slider_clicks += 1
                    page.state["on"] = not page.state["on"]
            self._slider = Slider()

        def evaluate(self, js, arg=None):
            # The autopick probe is the one that passes a selector.
            if arg is not None or "checkbox" in str(js):
                return self.state["on"]
            return self.ok

        def query_selector(self, _sel):
            return self._slider

    def _session_yielding(self, page):
        class S:
            def __init__(_s):
                _s.exited = False

            def __enter__(_s):
                return page

            def __exit__(_s, *a):
                _s.exited = True
                return False
        return S

    def _build(self, monkeypatch, page):
        monkeypatch.setattr(sdsession, "authenticate", lambda p: True)
        said = []
        br = sdbrowser.Browser("D", _session_cls=self._session_yielding(page),
                               _now=lambda: 1000.0, _log=said.append)
        br.page()
        return br, said

    def test_a_session_that_finds_autopick_on_turns_it_off(self, monkeypatch):
        page = self.TogglePage(on=True)
        _, said = self._build(monkeypatch, page)
        assert page.state["on"] is False
        assert page.slider_clicks == 1

    def test_it_says_so_rather_than_silently_undoing_the_platform(
            self, monkeypatch):
        """An agent that quietly reverses something Sleeper did is one you
        cannot debug afterwards."""
        _, said = self._build(monkeypatch, self.TogglePage(on=True))
        assert any("AUTO-PICK" in m for m in said), said

    def test_autopick_already_off_is_left_alone(self, monkeypatch):
        """Toggling a control that is already right would turn it ON."""
        page = self.TogglePage(on=False)
        _, said = self._build(monkeypatch, page)
        assert page.slider_clicks == 0
        assert not any("AUTO-PICK" in m for m in said)
