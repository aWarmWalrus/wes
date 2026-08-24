"""Pre-flight tests, and the seat claim it can now make (#039).

`wes_sleeper.join_draft` was wired up by the sleeperdraft migration and then
called by nothing at all: the pre-flight could tell you "X has NOT joined draft
Y — join it first" but had no way to do it, so getting into a mock meant
claiming the seat by hand. `--join` closes that, and these pin the part worth
being careful about: it is a WRITE, so it must never happen unasked.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "pc"))

import sleeper_draft_day as day  # noqa: E402
import wes_draft_agent  # noqa: E402
import wes_execute  # noqa: E402
import wes_sleeper  # noqa: E402
from sleeperdraft import config as sd_config  # noqa: E402

LEAGUE = {"name": "L", "status": "pre_draft", "draft_id": "OURS"}
ELSEWHERE = "SOMEWHERE_ELSE"


@pytest.fixture
def wired(monkeypatch):
    """Enough of the world for preflight to reach the seat block.

    The browser probe, the snapshot and the model all sit AFTER it, so this
    stops short of them: `_probe_browser=False` plus the calls below is the
    smallest arrangement that exercises the branch under test."""
    monkeypatch.setattr(sd_config, "TOKEN", "t" * 10)
    monkeypatch.setattr(wes_execute, "writes_enabled", lambda: True)
    monkeypatch.setattr(wes_sleeper, "league", lambda _id: dict(LEAGUE))
    # STUB THE MODEL. preflight really does ask gemma4:12b whether it is alive,
    # which is right on draft day and wrong in a unit test: unstubbed it cost
    # ~20s a case and turned five tests into a 105s suite.
    monkeypatch.setattr(wes_draft_agent, "_ask_model",
                        lambda *a, **k: {"player_key": "1"})
    # NO REAL SLEEPING. The seat check retries 12 times at 8s while draft_order
    # catches up — correct on draft day, and 96 seconds inside a unit test that
    # stubs the seat as never appearing.
    monkeypatch.setattr(day.time, "sleep", lambda _s: None)
    return monkeypatch


def _lines(monkeypatch, **kw):
    _ok, lines = day.preflight(
        league_id="L", username="u", draft_id=ELSEWHERE,
        _probe_browser=False, **kw)
    return "\n".join(lines)


class TestJoinIsOptIn:
    def test_it_does_not_claim_a_seat_unless_asked(self, wired):
        """The default path must not write. A pre-flight that quietly claims a
        seat is not a pre-flight — and in a real draft room a stray claim is
        visible to everyone else in it."""
        called = []
        wired.setattr(wes_sleeper, "slot_in_draft",
                      lambda *a, **k: None)
        wired.setattr(wes_sleeper, "join_draft",
                      lambda *a, **k: called.append(a) or 1)
        out = _lines(wired)
        assert called == [], "joined without being asked"
        assert "has NOT joined" in out

    def test_join_claims_a_seat_when_we_hold_none(self, wired):
        """Not seated before, seated after — which is also what the seat check
        below then confirms, so this walks the same path a real claim does."""
        got, seat = [], {"v": None}

        def slot(*_a, **_k):
            return seat["v"]

        def claim(d, u):
            got.append((d, u))
            seat["v"] = 4          # draft_order catches up
            return 4
        wired.setattr(wes_sleeper, "slot_in_draft", slot)
        out = _lines(wired, join=True, _join_fn=claim)
        assert got == [(ELSEWHERE, "u")]
        assert "join: claimed slot 4" in out
        assert "seat: u holds slot 4" in out

    def test_it_does_not_claim_a_SECOND_seat(self, wired):
        """Re-running must not take another seat. join_draft guards this too,
        but the cheap check belongs here: not calling it at all is better than
        relying on it to decline."""
        got = []
        wired.setattr(wes_sleeper, "slot_in_draft", lambda *a, **k: 3)
        out = _lines(wired, join=True, _join_fn=lambda d, u: got.append(1) or 9)
        assert got == []
        assert "join: already holds slot 3" in out

    def test_a_failed_claim_fails_the_preflight(self, wired):
        """Better to stop than to start a draft loop watching a seat we do not
        hold — that once produced a run of zero picks."""
        wired.setattr(wes_sleeper, "slot_in_draft", lambda *a, **k: None)

        def boom(_d, _u):
            raise RuntimeError("no free seat")
        ok, lines = day.preflight(
            league_id="L", username="u", draft_id=ELSEWHERE,
            _probe_browser=False, join=True, _join_fn=boom)
        assert ok is False
        assert "FAIL" in "\n".join(lines) and "no free seat" in "\n".join(lines)

    def test_a_fresh_claim_is_trusted_over_a_lagging_draft_order(self, wired):
        """join_draft verifies against the DOM precisely BECAUSE draft_order
        lags. Re-deriving the seat from the slow source and failing on it is
        the mistake join_draft was itself fixed for. Observed live: a claim
        that had landed was reported as "has NOT joined" and refused to draft
        (2026-08-24)."""
        wired.setattr(wes_sleeper, "slot_in_draft", lambda *a, **k: None)
        ok, lines = day.preflight(
            league_id="L", username="u", draft_id=ELSEWHERE,
            _probe_browser=False, join=True, _join_fn=lambda d, u: 4)
        out = "\n".join(lines)
        assert "join: claimed slot 4" in out
        # Scoped to the SEAT line. Asserting no FAIL anywhere would also catch
        # the later draft fetch, which 404s on this fixture's fake id and has
        # nothing to do with the behaviour under test.
        seat = [ln for ln in lines if "seat:" in ln]
        assert seat and "[ok ]" in seat[0], seat
        assert "holds slot 4" in seat[0]
        assert "has NOT joined" not in out

    def test_without_a_claim_the_retry_still_guards_the_seat(self, wired):
        """The trust above must not become a blanket pass: a run that did NOT
        join has only draft_order to go on, and an unjoined draft is still a
        hard fail — watching a seat that is not ours once produced zero picks.
        """
        wired.setattr(wes_sleeper, "slot_in_draft", lambda *a, **k: None)
        ok, lines = day.preflight(
            league_id="L", username="u", draft_id=ELSEWHERE,
            _probe_browser=False, join=False)
        assert ok is False
        assert "has NOT joined" in "\n".join(lines)

    def test_the_seat_check_reads_uncached(self, wired):
        """draft_order lags a claim (13.4s, measured 2026-08-24), so the
        already-seated check has to bypass the cache or a re-run inside the
        window sails past it and claims a second seat."""
        seen = []
        wired.setattr(wes_sleeper, "slot_in_draft",
                      lambda *a, **k: seen.append(k.get("max_age")) or 1)
        _lines(wired, join=True, _join_fn=lambda d, u: 1)
        assert seen and seen[0] == 0
