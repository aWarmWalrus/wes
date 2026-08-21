r"""Qualitative player notes — the things a stat line does not say (#040).

WHAT THIS IS AND IS NOT
Everything here is DERIVED FROM DATA WE HOLD: fields already in Sleeper's
player dump, and production we already fetch per season. Nothing is written by
a model, and nothing is an opinion about a person.

That boundary is deliberate. The obvious next step — asking a model to write
"attitude and character" notes on named professional athletes, storing them as
facts, and feeding them to a bot that posts in a group chat — is a different
kind of thing entirely. A small model will confabulate, an invented claim about
a real person does not self-correct, and it would go out under the owner's name.
That work is #041 and it needs sourced, dated, citable retrieval before any of
it is written down. This module deliberately stops short of it.

WHAT IT PRODUCES
  injury_note   "PUP — Knee - ACL (Surgery); last news 4 days ago"
  trajectory    "26, 5th year — 14.2 -> 11.8 -> 9.4 pts/g, declining"
  role_note     "RB2 behind Saquon Barkley"

All three are strings a human could check against the source in one step, which
is the property #041 will have to preserve if it ever writes anything softer.

PURE. No network, no clock — the caller passes `now` if it wants relative
times, so the same inputs always give the same output (docs/data-architecture.md
layer 3).
"""

# How long since the last news item before it is worth mentioning at all. Under
# a day is just "current" and adds noise to every line.
_FRESH_NEWS_S = 24 * 3600

_SEVERITY = {
    "IR": "out for the season unless activated",
    "PUP": "cannot practise yet",
    "Out": "ruled out",
    "Doubtful": "unlikely to play",
    "Questionable": "game-time decision",
    "Suspended": "suspended",
}


def _ago(then_ms, now):
    """Human relative time, or "" if unknown. Never guesses a date."""
    if not then_ms or not now:
        return ""
    days = (now - then_ms / 1000.0) / 86400.0
    if days < 1:
        return "today"
    if days < 2:
        return "yesterday"
    if days < 14:
        return f"{int(days)} days ago"
    if days < 60:
        return f"{int(days / 7)} weeks ago"
    return f"{int(days / 30)} months ago"


def injury_note(info, now=None):
    """One line on what is actually wrong, or "" for a healthy player.

    The point is that "PUP" and "PUP — Knee - ACL (Surgery)" are different
    facts, and the difference decides whether you draft him. We were storing
    only the first."""
    status = (info.get("injury_status") or "").strip()
    if not status:
        return ""
    bits = [status]
    part = (info.get("injury_body_part") or "").strip()
    # "Undisclosed" is Sleeper's placeholder for "we do not know" and adds
    # nothing to the word already printed.
    if part and part.lower() != "undisclosed":
        note = (info.get("injury_notes") or "").strip()
        bits.append(f"— {part}" + (f" ({note})" if note else ""))
    elif (info.get("injury_notes") or "").strip():
        bits.append(f"— {info['injury_notes'].strip()}")
    else:
        bits.append("— no detail given")
    seen = _ago(info.get("news_updated"), now)
    # Only worth saying when the news is OLD. Stale news on an injured player
    # is information: nothing for three weeks reads very differently from a
    # report this morning.
    line = " ".join(bits)
    if seen and now and info.get("news_updated"):
        if (now - info["news_updated"] / 1000.0) > _FRESH_NEWS_S:
            # Appended, not joined: " ".join put a space before the semicolon.
            line += f"; last news {seen}"
    return line


def severity(info):
    """Plain-English gloss of the status, or "". For a reader, not a score."""
    return _SEVERITY.get((info.get("injury_status") or "").strip(), "")


def trajectory(info, seasons):
    """Career arc from real per-game production. "" when we cannot say.

    `seasons` is [(year, points_per_game), ...] oldest first. Two points is
    enough for a direction; one is not, and an unknown arc must read as unknown
    rather than "flat"."""
    age, exp = info.get("age"), info.get("years_exp")
    who = []
    if age:
        who.append(str(age))
    if exp is not None:
        who.append("rookie" if exp == 0 else f"{_ordinal(exp + 1)} year")
    head = ", ".join(who)

    pts = [p for _y, p in (seasons or []) if p is not None]
    if len(pts) < 2:
        return head or ""
    arc = " -> ".join(f"{p:.1f}" for p in pts)
    # DIRECTION COMES FROM THE MOST RECENT MOVE, not first-against-last.
    # Comparing the ends called Puka Nacua "declining" on 28.4 -> 10.2 -> 21.9:
    # arithmetically true and useless, because what a manager wants to know is
    # that he bounced back. A middle year that swings hard gets said out loud
    # instead of being averaged away.
    prev, last = pts[-2], pts[-1]
    if last > prev * 1.15:
        shape = "trending up"
    elif last < prev * 0.85:
        shape = "trending down"
    else:
        shape = "steady"
    if len(pts) > 2 and max(pts) > 2 * max(min(pts), 0.1):
        shape = "volatile, " + shape
    tail = f"{arc} pts/g, {shape}"
    return f"{head} — {tail}" if head else tail


def role_note(info, teammates):
    """Where he sits on his own depth chart, in words.

    `teammates` is the same-team, same-position players as
    [{"name", "depth_chart_order"}]. Returns "" when the chart is unknown —
    25% of rostered skill players have no order, and inventing one would be
    worse than silence."""
    order = info.get("depth_chart_order")
    pos = (info.get("positions") or [None])[0]
    if not order or not pos:
        return ""
    if order == 1:
        return f"{pos}1 — starter"
    ahead = sorted(
        (t for t in (teammates or [])
         if isinstance(t.get("depth_chart_order"), int)
         and t["depth_chart_order"] < order),
        key=lambda t: t["depth_chart_order"])
    if not ahead:
        return f"{pos}{order}"
    return f"{pos}{order} behind {ahead[0].get('name')}"


def notes_for(info, seasons=None, teammates=None, now=None):
    """Every note we can honestly make about one player. Empty ones omitted."""
    out = {}
    for key, val in (("injury", injury_note(info, now=now)),
                     ("severity", severity(info)),
                     ("trajectory", trajectory(info, seasons)),
                     ("role", role_note(info, teammates))):
        if val:
            out[key] = val
    return out


def _ordinal(n):
    if 10 <= n % 100 <= 20:
        return f"{n}th"
    return f"{n}{ {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th') }"
