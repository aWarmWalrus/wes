"""A launched Chrome holding the Sleeper login, and the token injection.

Sleeper's public API is read-only, so anything that CHANGES a draft has to go
through the web app. This is the only place that starts a browser.

`config.TOKEN` is read at CALL time rather than captured at import, so a host
process (an MCP server, say) can set the account after this module is loaded
without having to reload it.
"""
import os

from . import config


class Session:
    """A persistent browser context holding the Sleeper login.

        with Session() as page:
            page.goto(...)
    """

    # How many sessions are currently open. Class-level: the limit is per
    # PROCESS, not per instance.
    _live = 0

    def __init__(self, headless=None):
        self.headless = config.HEADLESS if headless is None else headless
        self._pw = None
        self._ctx = None

    def __enter__(self):
        from playwright.sync_api import sync_playwright
        # ONE AT A TIME. Playwright's sync API cannot have two instances live
        # in a thread, and the error it gives -- "Sync API inside the asyncio
        # loop" -- says nothing about the actual mistake. That mistake is easy
        # to make once a Browser can hold a session for a whole draft: a code
        # path that forgets to accept the held browser opens its own and
        # forfeits a pick (2026-08-21, caught in a full mock).
        if Session._live:
            raise RuntimeError(
                "a browser session is already open — pass the held "
                "sleeperdraft.browser.Browser through instead of opening a "
                "second one (Playwright's sync API allows only one per thread)")
        os.makedirs(config.PROFILE_DIR, exist_ok=True)
        self._pw = sync_playwright().start()
        launch = dict(
            headless=self.headless,
            args=["--disable-blink-features=AutomationControlled"],
            ignore_default_args=["--enable-automation"],
        )
        try:
            self._ctx = self._pw.chromium.launch_persistent_context(
                config.PROFILE_DIR, channel=config.BROWSER_CHANNEL or None,
                **launch)
        except Exception:  # noqa: BLE001 — no Chrome/Edge: bundled Chromium
            self._ctx = self._pw.chromium.launch_persistent_context(
                config.PROFILE_DIR, **launch)
        page = self._ctx.pages[0] if self._ctx.pages else self._ctx.new_page()
        # ACCEPT NATIVE DIALOGS. Sleeper confirms destructive actions with
        # window.confirm ("Are you sure you want to start the draft? This
        # action cannot be undone"), and Playwright AUTO-DISMISSES native
        # dialogs unless a handler is registered. That is invisible: the click
        # lands, nothing appears in the DOM (a native dialog is not in the
        # DOM), and nothing happens -- which was twice misread as "the app
        # ignores synthetic clicks" (2026-08-15).
        page.on("dialog", lambda d: d.accept())
        Session._live += 1
        return page

    def __exit__(self, *exc):
        try:
            try:
                if self._ctx:
                    self._ctx.close()
            finally:
                if self._pw:
                    self._pw.stop()
        finally:
            # ALWAYS release, even if closing threw. A leaked counter would
            # lock out every later session, which is worse than the bug it
            # guards against.
            Session._live = max(0, Session._live - 1)
        return False


def is_login_wall(url, body):
    """Sleeper bounces a signed-out request to `/?redirect=...&login=` rather
    than showing an error, so a scrape of a logged-out session returns a
    perfectly valid page about something else entirely. Detect it explicitly --
    silently parsing the marketing page would look like an empty roster."""
    return "login=" in (url or "") or "redirect=" in (url or "") \
        or "LOG IN" in (body or "")


def authenticate(page):
    """Put the account token where the web app looks for it.

    Must run with the ORIGIN already loaded -- localStorage is per-origin, so
    writing it from about:blank silently lands nowhere. Returns False if no
    token is configured, so callers can say something useful instead of
    presenting an anonymous browser and reporting a mysteriously empty roster.
    """
    if not config.TOKEN:
        return False
    page.goto(config.WEB, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(1500)
    page.evaluate("([k, v]) => window.localStorage.setItem(k, v)",
                  [config.TOKEN_KEY, config.TOKEN])
    return True


def logged_in(league_id, _session_cls=None):
    """Is the stored profile still signed in? (bool, detail).

    Never raises: this is the check you run to find out WHY something else
    failed, so it has to survive the failure it is diagnosing."""
    if not config.TOKEN:
        return False, ("no WES_SLEEPER_TOKEN in the environment — set it and "
                       "restart the process that needs it.")
    try:
        with (_session_cls or Session)() as page:
            authenticate(page)
            page.goto(f"{config.WEB}/leagues/{league_id}/team",
                      wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(4000)
            body = " ".join((page.inner_text("body") or "").split())
            if is_login_wall(page.url, body):
                return False, ("Sleeper bounced to the login page — the "
                               "token is missing or expired.")
            return True, f"signed in; team page loaded ({len(body)} chars)"
    except Exception as e:  # noqa: BLE001
        return False, f"couldn't check the Sleeper session: {e}"
