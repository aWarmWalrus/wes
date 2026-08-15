r"""A point-in-time SNAPSHOT of everything a draft board needs (ticket #039).

    & C:\Users\awarm\wes-pc\.venv\Scripts\python.exe C:\Users\awarm\wes\pc\wes_snapshot.py build
    & C:\Users\awarm\wes-pc\.venv\Scripts\python.exe C:\Users\awarm\wes\pc\wes_snapshot.py status

WHY A SNAPSHOT AND NOT A CACHE
The player index and season projections used to live in PROCESS memory, so every
run re-fetched them and the network sat on the critical path of a 2-minute draft
clock. Pre-warming moved the fetch a few seconds earlier; it did not remove it.
If ESPN is slow at 6pm on draft day there is no board at all.

WHY POINT-IN-TIME, RATHER THAN A LIVE MIRROR (owner decision, 2026-08-15)
A snapshot is REPRODUCIBLE. "The board I inspected on Tuesday" and "the board
that drafted on Saturday" are provably the same artifact, which is what makes it
possible to check the thing before trusting it — and checking is how the stale
2025-actuals valuation was caught at all. A live mirror is always current and
never quite examinable: it can change between the look and the draft.

The trade is real and accepted: a snapshot can go stale. So staleness is
REPORTED LOUDLY rather than silently repaired, because a surprise refresh would
give back exactly the reproducibility the snapshot exists to provide.

PC-local, like the ledger and teams.yaml — never the repo.
"""
import json
import os
import sys
import tempfile
import time

SNAPSHOT_PATH = os.environ.get(
    "WES_FANTASY_SNAPSHOT",
    os.path.join(os.path.expanduser("~"), "wes-pc", "fantasy_snapshot.json"))

# Past this, `status` and any caller that asks will call it stale. Not enforced:
# a stale board still drafts, and a stale board you know about beats no board.
STALE_AFTER_S = 7 * 24 * 3600


def build(season=None, _players_fn=None, _proj_fn=None, _byes_fn=None,
          _now=None, path=None):
    """Fetch everything a board needs and write it atomically.

    ATOMIC on purpose: a half-written snapshot discovered at draft time is worse
    than no snapshot, because the failure arrives as garbled data rather than an
    honest absence."""
    import wes_nfl
    import wes_schedule
    import wes_sleeper

    season = season or wes_nfl._default_season()
    now = _now if _now is not None else time.time()

    players = (_players_fn or wes_sleeper.players_index)()
    projections = (_proj_fn or wes_nfl.season_projections)(season)
    byes = (_byes_fn or wes_schedule.bye_weeks)(season)

    # The CROSSWALK: sleeper_id -> espn_id, resolved once here rather than
    # fuzzily per lookup under a draft clock. Sleeper stopped maintaining its
    # own espn_id for newer players (106/300 of its top 300), which made 13 of
    # the top 25 projected players invisible to the board (#039).
    import wes_players
    try:
        table, xreport = wes_players.build(_sleeper_fn=lambda: players)
        crosswalk = {r["sleeper_id"]: r["espn_id"]
                     for r in table.values()
                     if r.get("sleeper_id") and r.get("espn_id")}
    except Exception:  # noqa: BLE001 — advisory; the board falls back to names
        crosswalk, xreport = {}, {"error": "crosswalk build failed"}

    snap = {
        "created_at": now,
        "season": str(season),
        "counts": {"players": len(players or {}),
                   "projections": len(projections or []),
                   "byes": len(byes or {}),
                   "crosswalk": len(crosswalk)},
        "crosswalk_report": {k: (len(v) if isinstance(v, list) else v)
                             for k, v in (xreport or {}).items()},
        "crosswalk": crosswalk,
        "players": players or {},
        "projections": projections or [],
        "byes": byes or {},
    }

    target = path or SNAPSHOT_PATH
    os.makedirs(os.path.dirname(target), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(target), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(snap, f)
        os.replace(tmp, target)          # atomic on Windows and POSIX alike
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return snap


_loaded = {"at": 0.0, "snap": None}


def load(path=None, _now=None):
    """The snapshot, or None. Cached in-process, invalidated by file mtime, so a
    freshly built snapshot is picked up without restarting a long-running loop."""
    target = path or SNAPSHOT_PATH
    try:
        mtime = os.path.getmtime(target)
    except OSError:
        return None
    if _loaded["snap"] is not None and _loaded["at"] == mtime:
        return _loaded["snap"]
    try:
        with open(target, encoding="utf-8") as f:
            snap = json.load(f)
    except (OSError, ValueError):
        return None                      # unreadable reads as absent, not empty
    _loaded.update(at=mtime, snap=snap)
    return snap


def age_seconds(snap=None, _now=None):
    snap = snap if snap is not None else load()
    if not snap:
        return None
    now = _now if _now is not None else time.time()
    return max(0.0, now - float(snap.get("created_at") or 0))


def players(_fallback_fn=None):
    """The player index, from the snapshot when there is one.

    Falls back to a live fetch rather than failing: a missing snapshot should
    cost latency, not the draft."""
    snap = load()
    if snap and snap.get("players"):
        return snap["players"]
    import wes_sleeper
    return (_fallback_fn or wes_sleeper.players_index)()


def projections(_fallback_fn=None):
    snap = load()
    if snap and snap.get("projections"):
        return snap["projections"]
    import wes_nfl
    return (_fallback_fn or wes_nfl.season_projections)()


def byes(_fallback_fn=None):
    snap = load()
    if snap and snap.get("byes"):
        return snap["byes"]
    import wes_schedule
    return (_fallback_fn or wes_schedule.bye_weeks)(
        __import__("wes_nfl")._default_season())


def crosswalk(_fallback=None):
    """{sleeper_id: espn_id}. Empty when there is no snapshot — the board then
    falls back to name matching, which is worse but not broken."""
    snap = load()
    return (snap or {}).get("crosswalk") or (_fallback or {})


def describe(snap=None):
    """One human-readable line per fact. What you read before trusting it."""
    snap = snap if snap is not None else load()
    if not snap:
        return (f"No snapshot at {SNAPSHOT_PATH} — the board will fetch live, "
                f"which puts the network on the draft clock. Run: "
                f"wes_snapshot.py build")
    age = age_seconds(snap) or 0
    c = snap.get("counts") or {}
    stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(snap["created_at"]))
    stale = "  ** STALE **" if age > STALE_AFTER_S else ""
    return (f"snapshot {stamp} ({age / 3600:.1f}h old){stale}\n"
            f"  season      {snap.get('season')}\n"
            f"  players     {c.get('players')}\n"
            f"  projections {c.get('projections')}\n"
            f"  byes        {c.get('byes')} teams\n"
            f"  crosswalk   {c.get('crosswalk')} sleeper->espn\n"
            f"  path        {SNAPSHOT_PATH}")


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "build":
        t0 = time.time()
        snap = build()
        print(f"built in {time.time() - t0:.1f}s")
        print(describe(snap))
    else:
        print(describe())


if __name__ == "__main__":
    main()
