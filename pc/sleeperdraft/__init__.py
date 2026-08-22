"""sleeperdraft — drive a Sleeper draft room from Python.

Sleeper's public v1 API is READ-ONLY. Everything that changes a draft (claiming
a seat, making a pick, posting in chat, turning off auto-pick) has to go through
the web app, so this package is two halves:

    read.py     public JSON, no auth, no browser
    the rest    Playwright against the live draft room

WHAT MAKES THIS WORTH USING rather than writing yourself: the DOM here is full
of traps, and each one costs a real pick to find. A few that are handled:

  * The search box does NOT find players. "Amon-Ra St. Brown" returns zero
    rows; "Amon-Ra" returns a different, undraftable player. Use scrolling.
  * The player list is virtualised -- ~98,000px of content, ~59 rows in the
    DOM -- and programmatic scrollTop does not move it. Wheel events do.
  * Row handles go STALE. React re-renders constantly, and a detached row's
    draft button reports `disable` forever and swallows clicks. Re-query.
  * The claim-seat handler is on `.claim-text`, not the `.header-button`
    wrapper that looks like the button.
  * `draft_order` is eventually consistent, lagging a claim by up to ~73s.
  * Sleeper commits a pick ~15s after the click, so verify patiently.
  * AUTO-PICK silently turns itself ON after one missed pick and stays on, so
    one miss costs the whole draft, not one pick.
  * Native confirm() dialogs are auto-dismissed by Playwright unless you
    register a handler, which makes a working click look like a no-op.

EVERY WRITE VERIFIES ITSELF against the API afterwards, because on this site a
click that "did not throw" means nothing.

    import sleeperdraft as sd

    sd.draft_turn(draft_id, roster_id)      # cheap, no auth
    sd.join_draft(draft_id, slot=4)
    sd.submit_pick(draft_id, "4034", "Christian McCaffrey", slot=4)

Configure with WES_SLEEPER_TOKEN and WES_SLEEPER_USER (see config.py for how
to obtain a token, and for the per-account variables).
"""
from .browser import Browser
from .chat import parse_chat, read_chat, send_chat
from .config import BASE, USERNAME, WEB, read_token
from .pick import autopick_on, set_autopick, submit_pick
from .read import (draft, draft_picks, draft_status_fresh, draft_turn,
                   drafted_player_ids, drafted_player_ids_fresh, slot_in_draft,
                   slot_names, user_id)
from .seat import join_draft
from .session import Session, authenticate, logged_in
from .snake import next_pick_for_slot, picks_until_turn, slot_for_pick

__all__ = [
    "BASE", "WEB", "USERNAME", "read_token",
    "Session", "authenticate", "logged_in", "Browser",
    "draft", "draft_picks", "draft_status_fresh", "draft_turn",
    "drafted_player_ids", "drafted_player_ids_fresh",
    "user_id", "slot_in_draft", "slot_names",
    "slot_for_pick", "next_pick_for_slot", "picks_until_turn",
    "join_draft", "submit_pick", "autopick_on", "set_autopick",
    "read_chat", "send_chat", "parse_chat",
]
