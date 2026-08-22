"""Submitting a pick, and the AUTO-PICK toggle that will otherwise do it for
you.

This is the sharp end. Nearly every comment below is a scar: each one records a
specific way a click reported success while nothing happened, which is the
failure mode this whole package exists to prevent.

THE WRITE GATE. `writes_allowed` is a hook, not a constant, so a host can wire
its own kill switch in:

    from sleeperdraft import pick
    pick.writes_allowed = my_app.writes_enabled

It defaults to permitting writes -- a library that silently refuses to act is
worse than one that acts.
"""
import time

from . import config, read, session

# How long to wait for the draft button to enable once you believe it is your
# turn. Comfortably inside any real clock (120s in mocks, 600s on draft day) and
# far longer than the socket needs.
ENABLE_WAIT_TRIES = 20

# The AUTO-PICK toggle. Sleeper turns this ON BY ITSELF the moment you miss a
# pick, and it STAYS on until somebody clicks it off -- so one missed pick does
# not cost one pick, it costs the rest of the draft. That is what happened on
# 2026-08-21: pick 1 was lost to a disabled button, autopick engaged, and every
# later "pick" was Sleeper choosing instantly while our clicks landed on
# nothing. The loop reported success throughout, because the player it wanted
# had indeed been drafted -- by somebody.
#
# Structure mirrors the claim seat: the state is a hidden checkbox and the
# handler is on the `span.slider` beside it, not on any of the wrappers.
_AUTOPICK_BOX = ".autopick-toggle-container input[type=checkbox]"
_AUTOPICK_SLIDER = ".autopick-toggle-container span.slider"

# The player list is a ReactVirtualized grid: ~98,000px of content in a 371px
# viewport, with roughly 59 rows rendered at a time. Anyone outside that window
# is absent from the DOM entirely, and no amount of querySelector will find him.
#
# Programmatic scrollTop does NOT move a virtualised list -- measured, it stays
# put. A real wheel event does, and that is what a human uses anyway.
SCROLL_STEPS = 40
SCROLL_PX = 1500


def writes_allowed():
    """Whether this process may act on a live draft. Replace to add a switch."""
    return True


def norm_name(s):
    """Loose name key: case, punctuation and suffixes removed, so 'A.J. Brown'
    matches 'AJ Brown' and 'Marvin Harrison Jr.' matches 'Marvin Harrison Jr'."""
    s = (s or "").lower()
    for junk in (".", ",", "'", "-", " jr", " sr", " ii", " iii", " iv"):
        s = s.replace(junk, "")
    return " ".join(s.split())


def autopick_on(page):
    """Is this seat set to draft automatically? None if the control is absent.

    None means UNKNOWN and must not be read as False. A querySelector on a page
    that has not finished rendering returns null, and a guard that treated that
    as "autopick is off" let autopick run a whole draft (2026-08-22)."""
    return page.evaluate(
        "(sel) => { const c = document.querySelector(sel);"
        " return c ? !!c.checked : null; }", _AUTOPICK_BOX)


def set_autopick(page, on=False, _tries=6):
    """Force AUTO-PICK to `on`. Returns the state we ended up in.

    Verified by re-reading the checkbox, not by the click landing: this is a
    control whose whole purpose is to act instead of you, so believing a click
    you cannot confirm is the worst possible place to be optimistic."""
    for _ in range(_tries):
        cur = autopick_on(page)
        if cur is None or cur == on:
            return cur
        slider = page.query_selector(_AUTOPICK_SLIDER)
        if slider is None:
            return cur
        slider.click()
        page.wait_for_timeout(700)
    return autopick_on(page)


def _scroll_to_row(page, find_fn, steps=SCROLL_STEPS):
    """Wheel down the player list until `find_fn()` matches, or we run out.

    Returns the row or None. Stops early when the window stops moving, which
    means the bottom is reached and he is genuinely not in the list -- as
    opposed to merely not drawn yet, a distinction this module has paid for
    repeatedly."""
    lst = page.query_selector(".ReactVirtualized__Grid")
    if lst is None:
        return None
    box = lst.bounding_box()
    if not box:
        return None
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    last_marker = None
    for _ in range(steps):
        page.mouse.wheel(0, SCROLL_PX)
        page.wait_for_timeout(350)
        row = find_fn()
        if row is not None:
            return row
        first = page.query_selector(".player-rank-item2")
        marker = (first.inner_text() or "")[:40] if first else None
        if marker is not None and marker == last_marker:
            return None                    # bottomed out
        last_marker = marker
    return None


def click_pick(page, player_name, want, find_fn=None):
    """Find the player's ROW and click its draft control.

    Shared by the per-call and held-open paths deliberately: a fix applied to
    one copy and not the other is precisely how the held-open path would
    diverge from the behaviour that is actually tested."""
    # WAIT FOR THE LIST TO EXIST, don't assume a fixed 6s is enough. An
    # unrendered list and an absent player look identical to a query selector,
    # and reading the first as the second cost a pick: a player was declared
    # gone on the run's first (slowest) page load and taken by us one pick
    # later, so he had plainly been there all along (2026-08-15).
    try:
        page.wait_for_selector(".player-rank-item2", timeout=25000)
    except Exception:  # noqa: BLE001 — turned into a clearer error below
        pass
    page.wait_for_timeout(1500)   # let the rest of the window paint

    def _find_row():
        for r in page.query_selector_all(".player-rank-item2"):
            nm = r.query_selector(".name-wrapper")
            first = (nm.inner_text() or "").split("\n")[0] if nm else ""
            if nm and norm_name(first) == want:
                return r
        return None

    find_fn = find_fn or _find_row
    row = _find_row()
    if row is None:
        # OUTSIDE THE RENDERED WINDOW: scroll to him rather than search. The
        # search box looks like the answer and is not -- measured live,
        # "Amon-Ra St. Brown" returns ZERO rows and "Amon-Ra" returns Montana
        # Lemonious-Craig, a different and undraftable player whose row is
        # disabled. Clicking that fires no request, verification fails, and the
        # loop reports "clicked X but it never appeared" -- which cost most of
        # a draft while the button, the session and the socket were each blamed
        # in turn (2026-08-22).
        row = _scroll_to_row(page, _find_row)
    if row is None:
        # TWO CAUSES, and only one of them means "pick someone else". If the
        # list is empty the draft room never rendered and we know NOTHING about
        # this player -- saying "he is gone" there is a guess, and a caller will
        # substitute on it.
        if not page.query_selector_all(".player-rank-item2"):
            raise RuntimeError(
                "the draft room's player list never rendered — cannot tell "
                "whether anyone is available; refusing to pick blind")
        raise RuntimeError(
            f"{player_name!r} is not available in the draft room even "
            f"after searching — he has most likely just been taken. "
            f"Refusing to click a different row")

    btn = row.query_selector(".draft-button")
    if btn is None:
        raise RuntimeError(f"no draft control on {player_name}'s row")
    if "disable" in (btn.get_attribute("class") or ""):
        # WAIT FOR IT TO ENABLE, do not sample it once. The button is disabled
        # until the room has been told over its socket that it is your turn, and
        # a page loaded seconds earlier has not heard yet. Checking once cost
        # pick 1 of a live draft to autopick while it genuinely WAS our turn
        # (2026-08-21).
        for _ in range(ENABLE_WAIT_TRIES):
            page.wait_for_timeout(1000)
            # RE-QUERY THE DOCUMENT, do not poll a handle. The player list is a
            # virtualised React grid that re-renders constantly, so the `row`
            # captured a moment ago is soon DETACHED -- and a detached node
            # keeps its old class forever and swallows clicks. Polling it read
            # "disable" for twenty seconds while a fresh query of the same page
            # showed all 25 rows ENABLED, seconds apart (2026-08-22, measured
            # side by side on a live turn). Every "the button is disabled"
            # failure traced to this.
            fresh = find_fn()
            if fresh is not None:
                row = fresh
            btn = row.query_selector(".draft-button") or btn
            if "disable" not in (btn.get_attribute("class") or ""):
                break
        else:
            # TRY ANYWAY. Refusing made sense when a click that landed on
            # nothing could be mistaken for success -- but verification now
            # requires the pick's draft_slot to be OURS, so a no-op click fails
            # honestly a few seconds later.
            #
            # And the class is not trustworthy: on 2026-08-22, with 37 picks
            # made, pick 38 ours, and autopick confirmed OFF, the button still
            # read `disable` after twenty seconds. Refusing there forfeited a
            # pick we were entitled to make. A click that might work beats a
            # certainty of standing down.
            print(f"[sleeper] draft button still reads disabled after "
                  f"{ENABLE_WAIT_TRIES}s — clicking anyway; verification will "
                  f"catch it if it does nothing", flush=True)
    btn.scroll_into_view_if_needed()
    page.wait_for_timeout(300)
    btn.click()
    page.wait_for_timeout(1500)

    # START DRAFT opens a confirmation modal; assume this does too rather than
    # rediscovering it the expensive way (2026-08-15).
    for sel in ("button:has-text('Confirm')", "button:has-text('Draft')",
                "[class*='confirm'] button", "[class*='modal'] button"):
        el = page.query_selector(sel)
        if el and el.is_visible():
            el.click()
            break
    page.wait_for_timeout(3000)


def submit_pick(draft_id, player_key, player_name, _session_cls=None,
                _picks_fn=None, _sleep_fn=None, browser=None, slot=None):
    """Draft ONE player in the live draft room. Raises on any doubt.

    THE ID/NAME SEAM, and why this verifies afterwards. Callers reason in
    `player_id` -- exact, unambiguous, the id Sleeper itself hands back on
    every pick. **But the draft room's DOM contains no player id anywhere**: a
    row is `div.player-rank-item2` holding a `.name-wrapper` with a display
    name and a per-row `.draft-button`. So the click has to be matched by NAME,
    and names are the ambiguous thing (suffixes, punctuation, two players
    sharing one).

    That gap is closed the only honest way -- by reading the pick back from the
    API and confirming the id that actually got drafted is the id you intended.
    Never assume a click did what it looked like.

    Note EVERY row has its own `.draft-button`, so "click the draft button"
    would draft whoever happens to sit at the top of a re-sorting list. That is
    why this targets the ROW belonging to a named player and never a bare
    control.

    Pass `slot` whenever you know it. Without it, verification can only ask
    whether the player was drafted at all -- see below for what that cost.
    """
    if not writes_allowed():
        raise RuntimeError("Sleeper writes are off")

    want = norm_name(player_name)
    if browser is not None:
        # A held page is already in the draft room, but RELOAD it first: the
        # available list is the one thing a long-lived page cannot be trusted
        # about, and picking off a stale list is the whole class of bug this
        # module keeps paying for.
        click_pick(browser.refresh(), player_name, want)
    else:
        with (_session_cls or session.Session)() as page:
            if not session.authenticate(page):
                raise RuntimeError("no Sleeper token — cannot reach the draft")
            page.goto(f"{config.WEB}/draft/nfl/{draft_id}",
                      wait_until="domcontentloaded", timeout=60000)
            click_pick(page, player_name, want)

    # Verify by ID — the only thing that closes the name-matching gap.
    #
    # POLLED, and UNCACHED. Two ways the first version got this wrong, both of
    # which reported failure on a pick that had actually succeeded (2026-08-15,
    # live): Sleeper takes a moment to commit, so a single read ~3s after the
    # click can legitimately miss it; and a cached pick list can serve the
    # pre-write answer -- verifying against a cache is not verifying at all.
    #
    # A false "did it work?" is worse here than a slow yes: it invites a human
    # into a live draft to fix something that is not broken, and the obvious
    # fix (pick again) drafts twice.
    #
    # TWELVE attempts, not six. Sleeper commits a pick in about fifteen
    # seconds, and six attempts at 2.5s gave up at almost exactly that -- so a
    # pick that WORKED was reported as failed, the loop stood down, and
    # autopick took a turn we had already won. Measured by hand at the moment
    # of a live click: two POSTs fired and the pick appeared at ~15s
    # (2026-08-22). Waiting longer is cheap (30s against a 300-600s clock).
    for attempt in range(12):
        if attempt:
            (_sleep_fn or time.sleep)(2.5)
        picks = (_picks_fn or _picks_uncached)(draft_id) or []
        # BY US, not by anyone. The first version asked only whether the player
        # appeared in the pick list at all, so when our click missed and
        # ANOTHER MANAGER took him two picks later, it read their pick as ours
        # and reported success. Observed live: the log said "DRAFTED Trey
        # McBride" while McBride went to slot 3 at pick 23 and our pick 21 was
        # Lamar Jackson (2026-08-21).
        #
        # This is the check that was supposed to close the name-matching gap,
        # and a check that can be satisfied by someone else's action closes
        # nothing.
        for pk in picks:
            if str(pk.get("player_id")) != str(player_key):
                continue
            if slot is None or int(pk.get("draft_slot") or -1) == int(slot):
                return True
            raise RuntimeError(
                f"{player_name!r} was drafted, but by slot "
                f"{pk.get('draft_slot')} at pick {pk.get('pick_no')} — not by "
                f"us (slot {slot}). Our click did not land.")
    raise RuntimeError(
        f"clicked {player_name!r} but {player_key} never appeared in the "
        f"draft's picks after ~15s — check the draft room before assuming "
        f"either way")


def _picks_uncached(draft_id):
    """Picks with the cache bypassed, for post-write verification only."""
    return read._get(f"/draft/{draft_id}/picks", ttl=0)
