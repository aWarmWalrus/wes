"""Consensus breaks our own ties, and only our ties.

`adj_value` is a real number and a noisy one. In a shallow league replacement
level sits high, so VOR barely separates elite players: at pick 1 of a 6-team
mock the top four came out 10.22 / 10.20 / 9.80 / 9.47 — a spread this module
already calls too close to separate. Ordered by that alone the board put Puka
Nacua (consensus 4, listed questionable) second, and the model took him first
overall (2026-08-25).
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "pc"))

import wes_sleeper as sl  # noqa: E402


def _c(name, adj, mkt=None):
    return {"name": name, "adj_value": adj, "market_rank": mkt}


def _names(board, gap=None):
    kw = {} if gap is None else {"gap": gap}
    return [c["name"] for c in sl.consensus_within_close_calls(board, **kw)]


class TestConsensusBreaksTies:
    def test_the_real_pick_one_board(self):
        """The exact numbers from the draft that exposed this."""
        board = [_c("Gibbs", 10.22, 2), _c("Nacua", 10.20, 4),
                 _c("Chase", 9.80, 3), _c("Bijan", 9.47, 1)]
        assert _names(board) == ["Bijan", "Gibbs", "Chase", "Nacua"]

    def test_a_real_edge_is_not_overridden_by_consensus(self):
        """The guard that matters. A candidate our model rates clearly higher
        must stay on top however the market feels — otherwise this stops being
        a valuation engine and becomes an ADP reader."""
        board = [_c("OurGuy", 20.0, 40), _c("Consensus", 10.0, 1)]
        assert _names(board) == ["OurGuy", "Consensus"]

    def test_clusters_are_taken_from_the_top_down(self):
        """A player outside the leading cluster cannot be promoted into it, no
        matter how well the market rates him."""
        board = [_c("A", 10.0, 9), _c("B", 9.5, 8), _c("Far", 1.0, 1)]
        assert _names(board, gap=0.75) == ["B", "A", "Far"]

    def test_the_gap_is_measured_from_the_cluster_top(self):
        """Chained near-misses must not drag one cluster across the board:
        10.0 and 9.4 are together, 8.8 is 1.2 off the top and starts its own."""
        board = [_c("Top", 10.0, 5), _c("Near", 9.4, 4), _c("Next", 8.8, 1)]
        assert _names(board, gap=0.75) == ["Near", "Top", "Next"]

    def test_unranked_sorts_last_inside_its_cluster_only(self):
        """Unranked is not ranked-last overall — adj_value still decides which
        cluster he is in, and a strong unranked player stays ahead of a weak
        consensus favourite."""
        board = [_c("NoRank", 10.1, None), _c("Ranked", 10.0, 3),
                 _c("Weak", 5.0, 1)]
        assert _names(board) == ["Ranked", "NoRank", "Weak"]

    def test_it_survives_missing_values(self):
        board = [{"name": "X"}, _c("Y", 1.0, 2)]
        assert len(sl.consensus_within_close_calls(board)) == 2
        assert sl.consensus_within_close_calls([]) == []
        assert sl.consensus_within_close_calls(None) == []
