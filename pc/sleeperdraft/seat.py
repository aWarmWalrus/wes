"""Claiming a seat in someone else's draft."""
import time

from . import config, pick, read, session


def join_draft(draft_id, username=None, slot=None, _session_cls=None,
               _slot_fn=None, _sleep_fn=None):
    """Claim a free seat in a draft someone else created. Returns the slot.

    THE BUG, AND WHY IT TOOK TWO INVESTIGATIONS (2026-08-21)

    An empty seat renders as a `.draft-user-header` containing a
    `.header-button` wrapper, which contains `.claim-text` ("CLAIM") and
    `.header-text` ("Team 3"). **The onclick is on `.claim-text`. The wrapper
    has none.**

    Eleven gestures were aimed at the wrapper across two sessions -- Playwright
    click, the header itself, a JS click bypassing hit-testing, the second of
    two overlapping duplicates, a real mouse click at its centre, a text
    selector, a dispatched pointer sequence, a dispatched touch sequence,
    focus+Enter. Every one was correctly ignored by an element with no handler,
    and each null result suggested a more exotic cause: an overlay, React
    Native Web's responder system, a websocket transport, an authorisation
    state that only real logins get. All wrong.

    What found it in seconds was reading `el.onclick` across the seat's
    descendants instead of theorising about why the click "did not land". When
    a click does nothing, ask the DOM which element is listening before asking
    why the event failed.

    THE SECOND HALF: `draft_order` is EVENTUALLY consistent. The seat renders
    as ours immediately; the API can still say nothing for some seconds. A 20s
    verification window reported failure on a claim that had actually
    succeeded -- so even after the click was fixed, this would have looked
    broken.

    ALREADY IN IT IS A SUCCESS, NOT AN ERROR. Re-running must not claim a
    second seat, so the existing slot is checked first and returned unchanged.
    """
    name = username or config.USERNAME
    slot_fn = _slot_fn or read.slot_in_draft
    _sleep_fn = _sleep_fn or time.sleep

    have = slot_fn(draft_id, name)
    if have:
        return have
    if not pick.writes_allowed():
        raise RuntimeError("Sleeper writes are off")

    want = f"CLAIM TEAM {slot}" if slot else "CLAIM"
    with (_session_cls or session.Session)() as page:
        if not session.authenticate(page):
            raise RuntimeError("no Sleeper token — cannot reach the draft")
        page.goto(f"{config.WEB}/draft/nfl/{draft_id}",
                  wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(9000)

        def _labels():
            return [" ".join((h.inner_text() or "").split())
                    for h in page.query_selector_all(".draft-user-header")]

        # ALREADY SEATED? ASK THE DOM, NOT THE API. The cheap check above uses
        # `draft_order`, which lags the claim by over a minute -- so a re-run
        # inside that window sailed past it and claimed a SECOND seat. That is
        # the one outcome this function exists to prevent, and the first fix
        # for the lag reintroduced it at the other end.
        held = [i for i, t in enumerate(_labels()) if t.lower() == name.lower()]
        if held:
            return held[0] // 2 + 1

        found = None
        for h in page.query_selector_all(".draft-user-header"):
            txt = " ".join((h.inner_text() or "").split()).upper()
            if txt.startswith(want):
                # THE HANDLER IS ON `.claim-text`, NOT on the `.header-button`
                # wrapper. That single fact is the whole bug.
                found = (h.query_selector(".claim-text")
                         or h.query_selector(".header-text")
                         or h.query_selector(".header-button"))
                if found:
                    break
        if found is None:
            raise RuntimeError(
                f"no free seat matching {want!r} in draft {draft_id} — it is "
                f"either full or the seat labels have changed")
        found.click()

        # THE DOM IS THE AUTHORITY FOR "did the click land", and the API is
        # not. Measured: a claim that renders instantly took over 73 seconds to
        # appear in `draft_order`, so an API-only check reported failure on a
        # seat we already held -- and a false failure here invites a second
        # claim, which is the one outcome worth avoiding.
        for _ in range(10):
            page.wait_for_timeout(1500)
            mine = [i for i, t in enumerate(_labels())
                    if t.lower() == name.lower()]
            if mine:
                # Seats render as an adjacent PAIR per team, so the team number
                # is the index halved and one-based.
                return mine[0] // 2 + 1
        raise RuntimeError(
            f"clicked the free seat in draft {draft_id} but no seat shows "
            f"{name} — the claim did not take")
