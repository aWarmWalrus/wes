r"""Draft day: pre-flight, wait for the room to open, then hand off to the loop.

    & C:\Users\awarm\wes-pc\.venv\Scripts\python.exe ^
        C:\Users\awarm\wes\pc\sleeper_draft_day.py [--check] [--league <id>]

WHY THIS EXISTS RATHER THAN "just run sleeper_draft_run.py"
Every mock so far was launched from a throwaway script that hard-coded a draft
id, because a mock CREATES the draft it then joins. A real draft does not work
that way: the id already exists, the commissioner (or the clock) starts it, and
nothing about the day is ours to trigger. What IS ours is being connected and
correct when it opens — so this resolves the ids from the league rather than
taking them on faith, checks the things that have actually broken before, and
then waits.

WHAT IT DOES NOT DO: start the draft. `autostart` is off and Sleeper opens the
room on the scheduled time or when the commissioner says so. Starting someone
else's draft is not ours to do even if the button were reachable.

THE PRE-FLIGHT IS THE POINT. Every check here corresponds to a failure that has
already happened once:
  * no token          -> stood down on all 15 picks, cpu_autopick took them,
                         and the printed roster looked perfectly plausible
  * stale snapshot    -> valuations from a board built before the last week of
                         news; not wrong enough to crash, just wrong
  * writes disabled   -> the loop logs WOULD-take and never clicks
  * unreadable slot   -> we watched slot 1 while our seat was elsewhere
  * dead model        -> every pick silently falls back to the engine's sort
Better to fail all of them at 12:30 than one of them at 13:00.
"""
import argparse
import sys
import time

import sleeper_draft_run
import wes_execute
import wes_sleeper
import wes_snapshot

LEAGUE = "1393935116232818688"          # Alloy Agents vs. Humans
USERNAME = wes_sleeper.USERNAME       # WES_SLEEPER_USER overrides

# How stale a snapshot may be before draft day is a bad time to find out. The
# thing takes ~5s to rebuild, so there is no reason to run on a week-old board.
SNAPSHOT_MAX_AGE_S = 36 * 3600


def preflight(league_id=LEAGUE, username=USERNAME, draft_id=None,
              _probe_browser=True):
    """Everything that must be true before the clock matters.

    Returns (ok, lines). Never raises: a pre-flight that dies on its first
    problem tells you about one thing when you wanted the list."""
    lines, ok = [], True

    def check(label, good, detail):
        nonlocal ok
        lines.append(f"  [{'ok ' if good else 'FAIL'}] {label}: {detail}")
        if not good:
            ok = False
        return good

    check("token", bool(wes_sleeper.TOKEN),
          f"{len(wes_sleeper.TOKEN)} chars" if wes_sleeper.TOKEN
          else "no WES_SLEEPER_TOKEN — writes cannot happen")

    check("live writes", wes_execute.writes_enabled(),
          "enabled" if wes_execute.writes_enabled()
          else "DISABLED — the loop will log WOULD-take and never click")

    try:
        lg = wes_sleeper.league(league_id) or {}
        # The league is still checked even when drafting elsewhere: it supplies
        # the SCORING, and a mock has none of its own.
        check("league", bool(lg.get("draft_id")),
              f"{lg.get('name')!r} status={lg.get('status')} "
              f"draft={lg.get('draft_id')}")
        draft_id = draft_id or lg.get("draft_id")
    except Exception as e:  # noqa: BLE001 — report, do not abort the rest
        check("league", False, f"{type(e).__name__}: {e}")
        return ok, lines

    try:
        if draft_id and draft_id != (lg or {}).get("draft_id"):
            # Drafting somewhere other than our league: the seat comes from the
            # draft itself, and NOT having one is a hard fail — an unjoined
            # draft would leave us watching a seat that is not ours, which once
            # produced a run of zero picks.
            roster_id = wes_sleeper.slot_in_draft(draft_id, username)
            check("seat", roster_id is not None,
                  f"{username} holds slot {roster_id}" if roster_id
                  else f"{username} has NOT joined draft {draft_id} - join it "
                       f"first, the seat is claimed on joining")
        else:
            roster_id = wes_sleeper.find_roster_id(league_id, username)
            check("roster", roster_id is not None,
                  f"{username} is roster_id {roster_id}" if roster_id is not None
                  else f"no roster for {username!r} in that league")
    except Exception as e:  # noqa: BLE001
        check("roster", False, f"{type(e).__name__}: {e}")
        roster_id = None

    try:
        d = wes_sleeper.draft(draft_id) or {}
        s = d.get("settings") or {}
        started = (d.get("start_time") or 0) / 1000
        when = (time.strftime("%Y-%m-%d %H:%M", time.localtime(started))
                if started else "unscheduled")
        lines.append(f"  [ -- ] draft: status={d.get('status')} {when} "
                     f"{s.get('teams')} teams x {s.get('rounds')} rounds, "
                     f"{s.get('pick_timer')}s clock, "
                     f"cpu_autopick={s.get('cpu_autopick')}")
        # The seat is assigned when the draft starts, so an unpublished order
        # is NORMAL pre-draft and must not read as a failure. It is still worth
        # printing, because reading it wrong is how we watched the wrong slot.
        slot_of = {str(v): int(k)
                   for k, v in (d.get("slot_to_roster_id") or {}).items()}
        slot = slot_of.get(str(roster_id)) if roster_id is not None else None
        seat = slot if slot else ("not yet assigned (published when the "
                                  "draft starts)")
        lines.append(f"  [ -- ] slot: {seat}")
    except Exception as e:  # noqa: BLE001
        check("draft", False, f"{type(e).__name__}: {e}")

    try:
        age = wes_snapshot.age_seconds()
        fresh = age is not None and age < SNAPSHOT_MAX_AGE_S
        check("snapshot", fresh,
              f"{age / 3600:.1f}h old" if age is not None else "MISSING"
              if age is None else "")
        for ln in wes_snapshot.describe().splitlines()[1:]:
            lines.append(f"         {ln.strip()}")
    except Exception as e:  # noqa: BLE001
        check("snapshot", False, f"{type(e).__name__}: {e}")

    # The model. A dead Ollama does not crash anything — every pick just falls
    # back to the engine's sort and the draft looks fine, which is precisely
    # why it needs checking rather than discovering.
    import wes_draft_agent
    got = wes_draft_agent._ask_model(
        {"context": {"phase": "starters"}, "shortlist": [
            {"player_key": "1", "name": "Test Player", "position": "RB"}]})
    check("model", isinstance(got, dict) and got.get("player_key") == "1",
          f"{wes_draft_agent.PICK_MODEL} answered"
          if isinstance(got, dict) else
          f"{wes_draft_agent.PICK_MODEL} gave no usable reply — every pick "
          f"would fall back to the engine")

    if _probe_browser and wes_sleeper.TOKEN:
        # The session is the single most likely thing to be broken on the day,
        # and the only check here that cannot be done from the API.
        try:
            with wes_sleeper._Session() as page:
                check("browser session", wes_sleeper.authenticate(page),
                      "authenticated" if wes_sleeper.authenticate(page)
                      else "token rejected — it has probably expired")
        except Exception as e:  # noqa: BLE001
            check("browser session", False, f"{type(e).__name__}: {e}")

    return ok, lines


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true",
                    help="pre-flight only; do not wait or draft")
    ap.add_argument("--draft", default=None,
                    help="a specific draft id (a MOCK, or someone else's "
                         "draft you have joined). Default: the league's own "
                         "draft. The seat is read from the draft, not the "
                         "league, because a mock has no league at all.")
    ap.add_argument("--league", default=LEAGUE,
                    help="the league whose SCORING to value under. A mock has "
                         "league_id null and no scoring of its own, so this "
                         "stays pointed at the real league even for mocks.")
    ap.add_argument("--username", default=USERNAME)
    ap.add_argument("--wait-hours", type=float, default=8.0,
                    help="how long to wait for the room to open")
    ap.add_argument("--no-browser-probe", action="store_true")
    ap.add_argument("--banter", choices=("off", "propose", "auto"),
                    default="off")
    ap.add_argument("--banter-gap", type=float, default=None)
    ap.add_argument("--rebuild-snapshot", action="store_true",
                    help="rebuild the local board first (~5s). The staleness "
                         "check is the one that fails routinely, and the fix "
                         "is one call - do it here rather than making the "
                         "owner remember a second command.")
    a = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    print(f"=== WES draft day pre-flight "
          f"{time.strftime('%Y-%m-%d %H:%M:%S')} ===")
    if a.rebuild_snapshot:
        meta = wes_snapshot.build()
        print(f"  rebuilt snapshot: {meta['counts']}")
    ok, lines = preflight(a.league, a.username, draft_id=a.draft,
                          _probe_browser=not a.no_browser_probe)
    print("\n".join(lines))
    if not ok:
        print("\nPRE-FLIGHT FAILED — not drafting. Fix the above and re-run.")
        return 1
    print("\npre-flight clean.")
    if a.check:
        return 0

    if a.draft:
        # An arbitrary draft: the seat lives in draft_order, keyed by user id.
        # `roster_id` and `slot` coincide for a mock (slot_to_roster_id is the
        # identity map), which is what the loop needs.
        draft_id = a.draft
        roster_id = wes_sleeper.slot_in_draft(draft_id, a.username)
        if roster_id is None:
            print(f"you have no seat in draft {draft_id} - JOIN it first "
                  f"(the seat is claimed on joining, and draft_order is empty "
                  f"until then)")
            return 1
    else:
        lg = wes_sleeper.league(a.league) or {}
        draft_id = lg.get("draft_id")
        roster_id = wes_sleeper.find_roster_id(a.league, a.username)

    # WAIT, do not start. Sleeper opens the room on its own schedule; our job
    # is to be connected when it does.
    # FAST, AND UNCACHED. Missing the start by even half a minute can cost the
    # first pick, and a missed pick engages Sleeper's autopick for the rest of
    # the draft. Five seconds against a 600s clock is nothing; the status read
    # is a couple of KB, so twelve a minute is ~1% of Sleeper's published rate
    # guidance. The LOG stays on a minute so a three-hour wait does not write
    # two thousand lines.
    deadline = time.time() + a.wait_hours * 3600
    said = 0.0
    while time.time() < deadline:
        status = wes_sleeper.draft_status_fresh(draft_id)
        if status == "drafting":
            break
        if status == "complete":
            print(f"draft {draft_id} is already complete — nothing to do")
            return 0
        if time.time() - said >= 60:
            said = time.time()
            print(f"[wait] {time.strftime('%H:%M:%S')} status={status}, "
                  f"holding")
        time.sleep(5)
    else:
        print("gave up waiting for the draft to open")
        return 1

    print(f"\ndraft {draft_id} is OPEN — handing off to the loop "
          f"(roster {roster_id})\n")
    print(sleeper_draft_run.run(draft_id, a.league, roster_id,
                                max_seconds=6 * 3600,
                                banter_mode=a.banter,
                                banter_gap=a.banter_gap))
    return 0


if __name__ == "__main__":
    sys.exit(main())
