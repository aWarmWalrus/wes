r"""Rebuild the local board, and REFUSE to install a bad one (#040).

    & C:\Users\awarm\wes-pc\.venv\Scripts\python.exe ^
        C:\Users\awarm\wes\pc\snapshot_refresh.py [--min-projections 250]

DAILY, NOT WEEKLY, and the reason is injuries. The owner asked for a weekly
refresh, but the field that actually moves is `injury_status` — a player ruled
out on Friday is a different draft and a different lineup by Sunday, and a
seven-day-old status is wrong for most of its life. Sleeper asks callers not to
pull the 14MB player dump more than once a day, which sets the other bound. So:
once a day, which is both the most useful and the most polite cadence.

WHY THIS EXISTS RATHER THAN A ONE-LINE `build()` IN A .ps1
Three of our four upstream sources are undocumented endpoints read
opportunistically. ESPN's `kona` projections and the statistics API can change
shape without notice, and the failure mode is not an exception — it is a
snapshot that writes cleanly with zero projections in it. The pre-flight checks
snapshot AGE, not contents, so a silently empty board would sail through it and
turn up as "no candidates" on the clock.

So this asserts what a usable snapshot looks like BEFORE replacing the good one,
and leaves the previous file in place if the new data is thin. A stale board
beats an empty one: stale is wrong at the edges, empty cannot draft at all.
"""
import argparse
import os
import shutil
import sys
import time

import wes_snapshot

# Floors, not targets. A healthy build is ~324 projections and ~12,200 players;
# these are set well below that so only a real collapse trips them.
MIN_PROJECTIONS = 250
MIN_PLAYERS = 5000
MIN_BYES = 30


def refresh(min_projections=MIN_PROJECTIONS, path=None, _build_fn=None,
            _log=print):
    """Build, validate, install-or-roll-back. Returns (ok, message)."""
    target = path or wes_snapshot.SNAPSHOT_PATH
    backup = target + ".prev"
    had_one = os.path.exists(target)
    if had_one:
        shutil.copy2(target, backup)

    t0 = time.time()
    try:
        meta = (_build_fn or wes_snapshot.build)(path=target)
    except Exception as e:  # noqa: BLE001 — a failed refresh must not leave a hole
        if had_one:
            shutil.copy2(backup, target)
        return False, f"build failed ({type(e).__name__}: {e}); kept the old one"

    counts = meta.get("counts") or {}
    problems = []
    if counts.get("projections", 0) < min_projections:
        problems.append(f"projections {counts.get('projections')} "
                        f"< {min_projections}")
    if counts.get("players", 0) < MIN_PLAYERS:
        problems.append(f"players {counts.get('players')} < {MIN_PLAYERS}")
    if counts.get("byes", 0) < MIN_BYES:
        problems.append(f"byes {counts.get('byes')} < {MIN_BYES}")

    if problems:
        # ROLL BACK. A thin snapshot is the dangerous outcome, because it looks
        # exactly like a healthy one to anything that checks age.
        if had_one:
            shutil.copy2(backup, target)
            return False, ("REJECTED: " + "; ".join(problems)
                           + " — kept the previous snapshot")
        return False, ("REJECTED: " + "; ".join(problems)
                       + " — and there was no previous snapshot to keep")

    _log(f"snapshot ok in {time.time() - t0:.0f}s: {counts}")
    return True, f"ok {counts}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-projections", type=int, default=MIN_PROJECTIONS)
    a = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print(f"=== snapshot refresh {time.strftime('%Y-%m-%d %H:%M:%S')} ===")
    ok, msg = refresh(min_projections=a.min_projections)
    print(msg)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
