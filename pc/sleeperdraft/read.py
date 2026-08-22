"""The READ half. No auth, no browser, no valuation.

Picks, order and settings are all public, which is worth knowing before you
reach for a browser: "who is gone, whose turn is it, which seat am I" is fully
answerable over plain HTTP. Only SUBMITTING a pick needs the session.

Every function takes `_get_fn` so a caller can test against fixtures without a
network, and every read states its own TTL -- the difference between "cached
for 60s" and "cache bypassed" is load-bearing here, and each docstring says
which it is and why.
"""
from . import config, fetch, snake


def _get(path, ttl=config.LEAGUE_TTL, _get_fn=None):
    return (_get_fn or fetch.get_json)(config.BASE + path, ttl=ttl)


def draft(draft_id, _get_fn=None):
    return _get(f"/draft/{draft_id}", ttl=60.0, _get_fn=_get_fn)


def draft_status_fresh(draft_id, _get_fn=None):
    """The draft's status, CACHE BYPASSED. For the pre-start wait only.

    `draft()` caches for 60s, and a wait loop polling on a 60s sleep would
    therefore gain nothing by polling faster -- the interval looked like the
    whole cause when it was half of it. Arriving 60s late to a 120s clock cost
    pick 1 of a live draft, which engaged autopick and cost the whole draft
    (2026-08-22)."""
    d = _get(f"/draft/{draft_id}", ttl=0, _get_fn=_get_fn)
    return (d or {}).get("status")


def draft_picks(draft_id, _get_fn=None):
    """Every pick made so far, oldest first. Short TTL: during a live draft
    this is the fast-moving fact everything else depends on."""
    return _get(f"/draft/{draft_id}/picks", ttl=15.0, _get_fn=_get_fn)


def drafted_player_ids_fresh(draft_id, _get_fn=None):
    """Taken ids, CACHE BYPASSED — for the check made immediately before a
    pick.

    `draft_picks` holds a 15s TTL, which is fine for polling and fatal here: a
    "re-check availability before submitting" rail reading a pick list up to 15
    seconds old sees a player taken moments ago as still available. That is
    exactly how a pick was wasted on a kicker who had already gone
    (2026-08-15). Verifying against a cache is not verifying."""
    picks = _get(f"/draft/{draft_id}/picks", ttl=0, _get_fn=_get_fn)
    if not isinstance(picks, list):
        return set()
    return {str(p.get("player_id")) for p in picks if p.get("player_id")}


def drafted_player_ids(draft_id, _get_fn=None):
    """Ids already taken by ANYONE.

    Matched by player_id, never by name: Sleeper hands out the exact id on
    every pick, and name matching is how a draft bot recommends someone who is
    already gone (two players share a name, or a suffix differs)."""
    picks = draft_picks(draft_id, _get_fn)
    if not isinstance(picks, list):
        return set()
    return {str(p.get("player_id")) for p in picks if p.get("player_id")}


def user_id(username, _get_fn=None):
    """Sleeper's account id for a display name. None if unknown.

    Needed for MOCK drafts, which have no league at all (`league_id` is null),
    so the roster route cannot be used to find a seat. A mock's `draft_order`
    is keyed by user id, and that is the only place the slot is written down."""
    u = _get(f"/user/{username}", _get_fn=_get_fn)
    return str(u.get("user_id")) if isinstance(u, dict) and u.get("user_id") \
        else None


def slot_in_draft(draft_id, username, _get_fn=None, _draft_fn=None):
    """Which draft SLOT `username` occupies. None if they are not in it.

    Works for mocks and league drafts alike, because it reads `draft_order`
    (user id -> slot) rather than the league's rosters. Sleeper writes this
    when the seat is claimed, which happens on JOINING -- so a draft you have
    not joined correctly returns None rather than a guess. Watching the wrong
    slot is not hypothetical: an early loop sat on slot 1 while the real seat
    was elsewhere and made zero picks (2026-08-15)."""
    uid = user_id(username, _get_fn=_get_fn)
    if not uid:
        return None
    d = (_draft_fn or draft)(draft_id) or {}
    order = d.get("draft_order") or {}
    slot = order.get(uid)
    return int(slot) if slot else None


def slot_names(draft_id, _get_fn=None, _draft_fn=None):
    """Draft slot -> the display name sitting in it. {} if unknown.

    Reads `draft_order` (user id -> slot) and resolves each id through
    `/user/{id}`, which takes an id as happily as a username -- so this works
    for MOCK drafts, which have no league and therefore no /users route.
    Best-effort per seat: one unresolvable id costs that name, not the map."""
    d = (_draft_fn or draft)(draft_id) or {}
    out = {}
    for uid, slot in (d.get("draft_order") or {}).items():
        if not slot:
            continue
        u = _get(f"/user/{uid}", _get_fn=_get_fn)
        name = u.get("display_name") if isinstance(u, dict) else None
        if name:
            out[int(slot)] = str(name)
    return out


def draft_turn(draft_id, roster_id, _get_fn=None):
    """CHEAP "is it my turn?" — two small API reads and some arithmetic.

    Deliberately holds no valuation and touches no browser, because a loop has
    to poll FAST. A poll that costs seconds is a poll that misses turns: a
    draft moving at ~4s per pick was over before an expensive loop ever saw its
    own turn (observed live 2026-08-15: 65 picks elapsed, 0 taken). Build the
    expensive board only once this says you are close.

    Returns a dict, or a plain-language string when the answer is "you can't
    know that yet" -- the caller is usually about to say it out loud.
    """
    try:
        d = draft(draft_id, _get_fn)
        picks = _get(f"/draft/{draft_id}/picks", ttl=3.0, _get_fn=_get_fn)
    except Exception as e:  # noqa: BLE001
        return f"I couldn't reach Sleeper's draft API ({e})."
    if not isinstance(d, dict):
        return "Sleeper returned no draft for that id."
    if d.get("status") == "complete":
        return "That draft is over."
    picks = picks if isinstance(picks, list) else []

    settings = d.get("settings") or {}
    teams = int(settings.get("teams") or 0)
    rounds = int(settings.get("rounds") or 0)
    reversal = int(settings.get("reversal_round") or 0)
    slot_of = {str(v): int(k)
               for k, v in (d.get("slot_to_roster_id") or {}).items()}
    my_slot = slot_of.get(str(roster_id))
    if not (teams and rounds and my_slot):
        return "That draft hasn't published its order yet."

    made = len(picks)
    wait = snake.picks_until_turn(my_slot, teams, made, rounds, reversal)
    return {"picks_made": made, "picks_until_turn": wait,
            "on_the_clock": wait == 0, "my_slot": my_slot, "teams": teams}
