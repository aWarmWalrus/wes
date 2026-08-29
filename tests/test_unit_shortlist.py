"""The shared shortlist: one statement of what we want, two readers.

The drafting agent built a valued board on the clock, used it once and threw it
away; the banter agent saw only finished picks. Everything forward-looking —
who we are chasing, who just got taken out from under us — was information the
process held and immediately discarded.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "pc"))

from sleeper import shortlist as wes_shortlist  # noqa: E402


def _cand(pid, name, pos="RB", vor=1.0, rank=None):
    return {"player_key": pid, "name": name, "positions": [pos],
            "vor": vor, "market_rank": rank}


class TestTargets:
    def test_targets_come_back_in_our_order(self):
        s = wes_shortlist.Shortlist()
        s.note_board([_cand("1", "First"), _cand("2", "Second")], pick_no=10)
        assert [t["name"] for t in s.targets()] == ["First", "Second"]
        assert s.targets()[0]["our_rank"] == 1

    def test_a_taken_player_stops_being_a_target(self):
        """Naming someone who went four picks ago reads as not watching the
        draft, and everyone in the room can see the board."""
        s = wes_shortlist.Shortlist()
        s.note_board([_cand("1", "First"), _cand("2", "Second")], pick_no=10)
        s.mark_taken(["1"])
        assert [t["name"] for t in s.targets()] == ["Second"]

    def test_no_board_yet_means_no_opinion(self):
        """Empty is correct before the first board — we have no view yet and
        should not manufacture one."""
        assert wes_shortlist.Shortlist().targets() == []

    def test_a_new_board_replaces_the_old_view(self):
        """Needs change as slots fill; a player wanted in round 2 can be
        irrelevant by round 9. The current view is the latest board, not an
        accumulation."""
        s = wes_shortlist.Shortlist()
        s.note_board([_cand("1", "Old")], pick_no=10)
        s.note_board([_cand("2", "New")], pick_no=20)
        assert [t["name"] for t in s.targets()] == ["New"]

    def test_mark_taken_tolerates_none_and_ints(self):
        s = wes_shortlist.Shortlist()
        s.note_board([_cand("7", "Seven")], pick_no=1)
        s.mark_taken([None, 7])          # ints arrive from the picks payload
        assert s.targets() == []


class TestMemoryOfWanting:
    def test_a_replaced_board_still_remembers_we_wanted_him(self):
        """The whole point: he is off the current board because somebody took
        him, and that is exactly when 'that's who I wanted' is worth saying."""
        s = wes_shortlist.Shortlist()
        s.note_board([_cand("1", "Target")], pick_no=10)
        s.note_board([_cand("2", "Other")], pick_no=20)
        assert s.wanted("1")["name"] == "Target"

    def test_it_keeps_the_BEST_rank_we_ever_gave_him(self):
        """Our number one in round 3 who slid to eighth by round 6 was still
        our number one, and that is the version worth saying out loud."""
        s = wes_shortlist.Shortlist()
        s.note_board([_cand("1", "Guy")], pick_no=10)                  # rank 1
        s.note_board([_cand("9", "X"), _cand("1", "Guy")], pick_no=20)  # rank 2
        assert s.wanted("1")["our_rank"] == 1

    def test_someone_never_shortlisted_is_None(self):
        """None means 'never on our list', not 'we rated him poorly'.
        Claiming to have wanted everybody is how a bot stops being believed."""
        s = wes_shortlist.Shortlist()
        s.note_board([_cand("1", "Guy")], pick_no=10)
        assert s.wanted("999") is None

    def test_lookup_survives_int_vs_str_ids(self):
        """Sleeper hands player ids back as strings in some payloads and the
        board carries them as strings too, but a caller passing an int should
        not silently get None."""
        s = wes_shortlist.Shortlist()
        s.note_board([_cand("42", "Guy")], pick_no=10)
        assert s.wanted(42)["name"] == "Guy"


class TestItNeverDoesWork:
    def test_note_board_tolerates_junk_without_fetching_anything(self):
        """A dumb store on purpose: it must never be the reason a board gets
        rebuilt, because that call is paid on the clock deliberately."""
        s = wes_shortlist.Shortlist()
        s.note_board(None, pick_no=1)
        s.note_board([{}, {"player_key": ""}], pick_no=2)
        assert s.targets() == []
        assert s.stats()["ever"] == 0
