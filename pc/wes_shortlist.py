"""What we want right now — shared by the agent that picks and the one that talks.

Both agents were reasoning about the same board and neither could see the
other's copy. The drafting agent built a valued shortlist on the clock, used it
once and threw it away; the banter agent had only the finished picks, so the
best it could do was react to what had already happened. Everything forward
looking -- who we are hoping falls to us, who just got taken off our list --
was information the process held and immediately discarded.

This holds it. Deliberately a DUMB STORE: it computes nothing, fetches nothing,
and never triggers a board rebuild. That last part is the constraint that
shapes it -- building the board is the expensive call, paid on the clock on
purpose, and a poll that costs seconds is a poll that misses turns. So this is
written to at pick time and read cheaply from anywhere, and it is never the
reason work happens.

Two views, because the questions are different:

  targets()  -- what we want NOW, still available, best first. Forward looking.
  wanted()   -- did we ever want this player, and how highly. Backward looking,
                and the thing that makes "that's exactly who I wanted" true
                rather than improvised.
"""


class Shortlist:
    """The live shortlist plus the memory of everyone who was ever on it.

    Not thread-safe and does not need to be: the draft loop is one thread, and
    the chat runs inside it between picks.
    """

    def __init__(self):
        self._current = []      # rows from the most recent board, our order
        self._ever = {}         # player_key -> the best row we ever held
        self._taken = set()
        self.updated_at_pick = None

    # -- writing ------------------------------------------------------------
    def note_board(self, candidates, pick_no=None):
        """Record a freshly computed board. Replaces the current view.

        REPLACES rather than merges, because a new board is a better statement
        of what we want than an accumulation of old ones -- needs change as
        slots fill, and a player we wanted in round 2 may be irrelevant by
        round 9. The MEMORY of having wanted someone is kept separately, which
        is the part that stays interesting after the board moves on.
        """
        rows = []
        for i, c in enumerate(candidates or [], start=1):
            pid = str(c.get("player_key") or "")
            if not pid:
                continue
            row = {
                "player_key": pid,
                "name": c.get("name"),
                "position": (c.get("positions") or [None])[0],
                "our_rank": i,
                "vor": c.get("vor"),
                "market_rank": c.get("market_rank"),
            }
            rows.append(row)
            # BEST rank we ever gave him, not the latest. Someone who was our
            # number one in round 3 and slid to eighth by round 6 was still our
            # number one, and that is the version worth saying out loud.
            prev = self._ever.get(pid)
            if prev is None or i < prev.get("our_rank", 10 ** 6):
                self._ever[pid] = dict(row, wanted_at_pick=pick_no)
        self._current = rows
        self.updated_at_pick = pick_no

    def mark_taken(self, player_ids):
        """Players now off the board. Cheap, and safe to call on every poll."""
        self._taken |= {str(p) for p in (player_ids or ()) if p is not None}

    # -- reading ------------------------------------------------------------
    def targets(self, limit=4):
        """Who we are hoping falls to us: still available, our order, best
        first. Empty before the first board is built, which is correct -- we
        have no opinion yet and should not pretend to one."""
        out = [r for r in self._current if r["player_key"] not in self._taken]
        return out[:limit]

    def wanted(self, player_key):
        """Our ranking of a player we once shortlisted, or None.

        None is the common answer and means exactly "he was never on our list"
        -- not "we did not rate him". The distinction matters: claiming to have
        wanted everybody is how a bot stops being believed."""
        return self._ever.get(str(player_key))

    def stats(self):
        return {"current": len(self._current), "ever": len(self._ever),
                "taken": len(self._taken),
                "updated_at_pick": self.updated_at_pick}
