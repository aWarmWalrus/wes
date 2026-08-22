"""Snake-draft position arithmetic. PURE — no network, no clock.

"Whose pick is this" and "when do I pick next" are questions a draft client is
useless without, and they are pure functions of the board shape. Kept separate
from anything that touches Sleeper so they can be tested, and trusted, on their
own.
"""


def slot_for_pick(pick_no, teams, reversal_round=0):
    """Which draft SLOT (1..teams) owns overall pick `pick_no` (1-indexed).

    Snake: odd rounds run 1->N, even rounds run N->1. `reversal_round` is
    Sleeper's third-round-reversal option (0 = off); when set, the snake flips
    an extra time from that round on, so rounds >= it invert the usual parity.
    """
    if teams <= 0 or pick_no <= 0:
        return None
    rnd = (pick_no - 1) // teams + 1
    idx = (pick_no - 1) % teams
    forward = (rnd % 2 == 1)
    if reversal_round and rnd >= reversal_round:
        forward = not forward
    return idx + 1 if forward else teams - idx


def next_pick_for_slot(slot, teams, picks_made, rounds, reversal_round=0):
    """The next overall pick number belonging to `slot`, or None if their draft
    is done. `picks_made` is how many picks have already happened."""
    for pick_no in range(picks_made + 1, teams * rounds + 1):
        if slot_for_pick(pick_no, teams, reversal_round) == slot:
            return pick_no
    return None


def picks_until_turn(slot, teams, picks_made, rounds, reversal_round=0):
    """How many picks until `slot` is on the clock (0 = on the clock now).

    This is the number that decides strategy: with 20 picks to wait you can let
    a run happen, with 1 you cannot."""
    nxt = next_pick_for_slot(slot, teams, picks_made, rounds, reversal_round)
    return None if nxt is None else nxt - picks_made - 1
