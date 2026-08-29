"""What the draft room looks like, assembled for the chat agent.

SPLIT OUT OF sleeper_draft_run. That module is the LOOP -- watch the clock,
decide, pick, verify -- and this is a hundred and forty lines of assembling a
payload for a different agent entirely. Reading either one meant paging past
the other.

Everything here is best-effort by design: a chat line is never worth breaking a
draft for, so the whole body sits under one broad except and a failure returns
the cheap fields rather than raising into the pick path.
"""
from sleeper import banter as wes_banter
from sleeper import data as wes_sleeper


def build(draft_id, league_id, roster_id, state, wait, board_fn,
          shortlist=None):
    """The context dict the banter agent reasons over.

    Banter had `{round, picks_made, picks_until_our_turn}` and nothing about
    who drafted what, so it could only produce generic ribbing. "That is your
    fourth tight end" needs the picks; "your RB1 is on PUP with an ACL" needs
    the notes. Both are things we already hold."""
    ctx = {"round": state.get("round"),
           "picks_made": state.get("picks_made"),
           "picks_until_our_turn": wait}
    try:
        import wes_notes
        import wes_snapshot
        idx = wes_snapshot.players()
        picks = wes_sleeper.draft_picks(draft_id) or []
        names = _slot_names(draft_id)
        teams, rounds = _draft_shape(draft_id)
        _add_targets(ctx, shortlist, picks)
        ctx["recent_picks"] = _recent_picks(picks, idx, names, roster_id,
                                            teams, rounds, shortlist)
        ctx["rosters_by_slot"] = _rosters_by_slot(picks, names, roster_id)
        ctx["our_slot"] = roster_id
        ctx["our_roster"] = _our_roster(picks, roster_id)
        hurt = _our_injuries(picks, idx, roster_id, wes_notes)
        if hurt:
            ctx["our_injuries"] = hurt
    except Exception:  # noqa: BLE001 — a chat line is not worth a draft
        pass
    return ctx


def _slot_names(draft_id):
    """Slot -> display name. WHOSE PICK IT WAS, said outright.

    Slot numbers alone made the model join `our_slot` against `draft_slot` to
    work out which picks were its own -- and it got that wrong in front of the
    owner, denying a player it had drafted itself (2026-08-22). Ownership is a
    fact we hold; it does not go to the model as a puzzle."""
    try:
        return wes_sleeper.slot_names(draft_id)
    except Exception:  # noqa: BLE001
        return {}


def _draft_shape(draft_id):
    """(teams, rounds). `rounds` bounds where consensus rank still means
    anything -- see pick_verdict. Without it a round-14 kicker reads as a
    reach."""
    try:
        s = ((wes_sleeper.draft(draft_id) or {}).get("settings") or {})
        return int(s.get("teams") or 0), int(s.get("rounds") or 0)
    except Exception:  # noqa: BLE001
        return 0, 0


def _add_targets(ctx, shortlist, picks):
    """WHO WE ARE HOPING FALLS TO US, from the board the drafting agent will
    actually pick from rather than a second guess at it.

    Freshened from the picks we already hold -- no extra fetch, and without it
    a target taken three picks ago still reads as somebody we are waiting on,
    which is a claim the room can see is wrong."""
    if shortlist is None:
        return
    shortlist.mark_taken(p.get("player_id") for p in picks)
    targets = shortlist.targets(4)
    if targets:
        ctx["our_targets"] = targets


def _name_of(pick):
    md = pick.get("metadata") or {}
    return " ".join(filter(None, [md.get("first_name"), md.get("last_name")]))


def _who(slot, names, roster_id):
    """The manager's name for a slot.

    A CPU seat has no user id in draft_order, so it has no display name -- and
    `None` reached the model AS the manager's name, giving "pick 42 None took
    Colston Loveland" (2026-08-24). Naming the seat is honest and still usable;
    inventing a manager would not be."""
    if slot == roster_id:
        return "US"
    return names.get(slot) or f"slot {slot}"


def _recent_picks(picks, idx, names, roster_id, teams, rounds, shortlist):
    """The last handful, each with what it was WORTH.

    Roster shapes gave the model one joke -- "five RBs and no TE" -- and it
    told that joke every time. Having an opinion about a PICK needs where the
    player was ranked, how far from consensus he went, and whether we wanted
    him ourselves."""
    out = []
    for p in picks[-6:]:
        pid = str(p.get("player_id"))
        row = {
            "pick": p.get("pick_no"), "slot": p.get("draft_slot"),
            "by": _who(p.get("draft_slot"), names, roster_id),
            "ours": p.get("draft_slot") == roster_id,
            "player": _name_of(p),
            "position": (p.get("metadata") or {}).get("position"),
            "round": p.get("round"),
        }
        rank = (idx.get(pid) or {}).get("search_rank")
        verdict = wes_banter.pick_verdict(p.get("pick_no"), rank, teams,
                                          rounds)
        if verdict:
            row.update(verdict)
        # SNIPED. A player who was on our shortlist and went to somebody else
        # is the most quotable thing that happens in a draft, and we already
        # know it. Never flagged on our OWN picks: "that's who I wanted" about
        # a player we just took reads as a bot that has lost track of itself.
        was = shortlist.wanted(pid) if shortlist is not None else None
        if not row["ours"] and was:
            row["we_wanted"] = True
            row["our_rank_for_him"] = was.get("our_rank")
        out.append(row)
    return out


def _rosters_by_slot(picks, names, roster_id):
    """Every team's shape, so it can needle the right person."""
    rosters = {}
    for p in picks:
        pos = (p.get("metadata") or {}).get("position")
        if pos:
            rosters.setdefault(p.get("draft_slot"), []).append(pos)
    return {_who(k, names, roster_id): _count(v)
            for k, v in sorted(rosters.items()) if k is not None}


def _our_roster(picks, roster_id):
    """Ours by name, as its own field. The model should never have to
    reconstruct what it drafted from a table of everyone's."""
    return [{"player": _name_of(p),
             "position": (p.get("metadata") or {}).get("position"),
             "round": p.get("round")}
            for p in picks if p.get("draft_slot") == roster_id]


def _our_injuries(picks, idx, roster_id, wes_notes):
    """Our walking wounded, in words. The most quotable facts in a draft room
    are usually about a body part."""
    out = []
    for p in picks:
        if p.get("draft_slot") != roster_id:
            continue
        info = idx.get(str(p.get("player_id"))) or {}
        note = wes_notes.injury_note(info)
        if note:
            out.append(f"{info.get('name')}: {note}")
    return out


def _count(items):
    out = {}
    for i in items:
        out[i] = out.get(i, 0) + 1
    return out
