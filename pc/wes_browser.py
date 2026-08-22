r"""A browser held open for the length of a draft, with honest restarts (#039).

WHY
Every browser action here paid a full launch: ~0.7s to start Chrome, ~2.1s to
authenticate, ~6s for the draft room to render. That is fine once a pick — it is
3% of a 600s clock — but the banter loop reads the chat on almost every poll,
so the same ~9s was being paid every twenty seconds to fetch a handful of
messages. Slow enough that the bot reads as sluggish in a live room.

WHAT THIS IS
One `Browser` that owns a Playwright context and a page parked on the draft
room, handed to callers for as long as it stays healthy. Reads become
sub-second; a pick keeps its own verification and loses only the startup.

WHY IT IS NOT SIMPLY FASTER
A page held open for two hours is a liability, and pretending otherwise is how
this breaks in the one place it must not. It can be navigated away by the app,
lose its websocket, have its React tree detached by a re-render, or be closed
under us. So the contract here is not "reuse the page", it is:

    ask for a page  ->  prove it is usable  ->  hand it over
                    ->  or throw it away and build a new one

`healthy()` is the load-bearing part, not `page`. It checks the object is alive,
the URL is still the draft room, we are not at a login wall, and a known
element of the draft room is present. Anything less would let a subtly broken
page through, and a subtly broken page during a draft is exactly the failure
mode this project keeps rediscovering: an action that lands on nothing and
reports success.

RECYCLING IS ROUTINE, NOT AN ERROR. The session is rebuilt on a failed health
check, after `MAX_USES` operations, or after `MAX_AGE_S` — proactively, so the
rebuild happens between picks rather than on the clock. A caller never sees it.

AND IT ALWAYS DEGRADES TO THE OLD BEHAVIOUR. If a page cannot be established
the caller gets a per-call session instead: slower, and exactly what shipped
before this module existed. A speed optimisation must never be able to stop a
draft.
"""
import time

import wes_sleeper

# Proactive recycling. Both are guesses, deliberately conservative: a draft is
# ~2h and a rebuild costs ~9s, so recycling every 20 minutes is cheap insurance
# against slow drift we would otherwise only notice as a weird failure.
MAX_AGE_S = 20 * 60
MAX_USES = 40


class Browser:
    """A draft-room page, kept alive and re-made when it is not trustworthy.

        br = Browser(draft_id)
        with br.page() as page:      # may be reused, may be brand new
            ...
        br.close()

    Not thread-safe, and does not need to be: the draft loop is one thread and
    the Chrome profile is a singleton, so two of these cannot exist anyway."""

    def __init__(self, draft_id, _session_cls=None, _now=None, _log=None):
        self.draft_id = draft_id
        self._session_cls = _session_cls or wes_sleeper._Session
        self._now = _now or time.time
        self._log = _log or (lambda _m: None)
        self._sess = None
        self._page = None
        self._born = 0.0
        self.uses = 0
        self.rebuilds = 0
        self.failures = 0

    # -- lifecycle ---------------------------------------------------------
    def _build(self):
        self._teardown()
        sess = self._session_cls()
        page = sess.__enter__()
        try:
            page.set_viewport_size({"width": 1600, "height": 1000})
            if not wes_sleeper.authenticate(page):
                raise RuntimeError("no WES_SLEEPER_TOKEN — cannot reach the "
                                   "draft")
            page.goto(f"{wes_sleeper.WEB}/draft/nfl/{self.draft_id}",
                      wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(6000)
            # Leave the room in a state where WE make the picks. Sleeper turns
            # AUTO-PICK on by itself after a missed pick and never turns it
            # back off, so one miss costs the rest of the draft rather than
            # one pick (2026-08-21).
            if wes_sleeper.autopick_on(page):
                wes_sleeper.set_autopick(page, False)
                self._log("browser: AUTO-PICK was ON — turned it off")
        except BaseException:
            # A half-built session must not be left holding the profile lock.
            try:
                sess.__exit__(None, None, None)
            except Exception:  # noqa: BLE001
                pass
            raise
        self._sess, self._page, self._born = sess, page, self._now()
        self.uses = 0
        self.rebuilds += 1
        self._log(f"browser: session #{self.rebuilds} up")
        return page

    def _teardown(self):
        if self._sess is not None:
            try:
                self._sess.__exit__(None, None, None)
            except Exception:  # noqa: BLE001 — closing must never raise onward
                pass
        self._sess = self._page = None

    def close(self):
        self._teardown()

    # -- health ------------------------------------------------------------
    def healthy(self):
        """Is the page one we can still trust? The load-bearing method.

        Deliberately checks more than "is the object alive". A page that has
        been bounced to the login wall, or navigated away, or had the draft
        room unmounted, is ALIVE and useless — and handing it to a caller
        produces an action that lands on nothing and reports success, which is
        the failure this codebase keeps rediscovering."""
        page = self._page
        if page is None:
            return False
        try:
            if page.is_closed():
                return False
            url = page.url or ""
            if self.draft_id not in url:
                return False
            if wes_sleeper._is_login_wall(url, ""):
                return False
            # A known element of the draft room. `evaluate` also proves the JS
            # context is still attached, which `url` alone does not.
            return bool(page.evaluate(
                "() => !!document.querySelector('.draft-board')"
                " || !!document.querySelector('.player-rank-item2')"
                " || !!document.querySelector('.draft-user-header')"))
        except Exception:  # noqa: BLE001 — any error means "do not trust it"
            return False

    def _stale(self):
        return (self._now() - self._born > MAX_AGE_S) or self.uses >= MAX_USES

    # -- use ---------------------------------------------------------------
    def page(self):
        """A usable page. Rebuilds first if the current one is not.

        Returns the page directly (not a context manager): the caller must not
        close it, because the whole point is that it outlives the call."""
        if self._page is not None and self._stale() and self.healthy():
            # PROACTIVE. Recycling on a schedule means the ~9s rebuild happens
            # between picks instead of on the clock.
            self._log(f"browser: recycling after {self.uses} uses")
            self._build()
        elif not self.healthy():
            if self._page is not None:
                self.failures += 1
                self._log(f"browser: unhealthy (failure #{self.failures}), "
                          f"rebuilding")
            self._build()
        self.uses += 1
        return self._page

    def refresh(self):
        """Reload the draft room in place — cheaper than a rebuild when the
        page is fine but the content has drifted."""
        page = self.page()
        page.goto(f"{wes_sleeper.WEB}/draft/nfl/{self.draft_id}",
                  wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(3000)
        return page

    def stats(self):
        return {"rebuilds": self.rebuilds, "failures": self.failures,
                "uses": self.uses,
                "age_s": round(self._now() - self._born, 1) if self._born
                else None}
