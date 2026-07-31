"""Scheduled entry point for the Fantasy GM cycle (#029 P3, ticket #005-adjacent —
see the note at the bottom of this docstring on why this is NOT #005 itself).

Runs `wes_execute.propose_lineup_change` for every configured team whose
autonomy is `propose` or `auto` (never `advise` — that mode never acts, so
running it here would just add noise: a scrape + ESPN fetch for a team that can
never move). Prints one line per team; the launcher script redirects that to a
log file, matching the house pattern (`run_nightly_eval.ps1`): **log-only, WES
never speaks on its own initiative** for a routine/no-op run.

The one exception, deliberately: if a team's autonomy is `auto`, live writes are
on, and this run ACTUALLY WRITES to Yahoo, that's printed with a distinct
`[EXECUTED]` marker so it's grep-able — this is the one outcome worth a human
noticing, even though nothing here pushes a notification yet (see #029's ticket
notes: DM-on-real-write is a known, deliberately deferred next step, not an
oversight).

Exit code is 1 if ANY team's cycle raised an exception (never 1 for a normal
"already optimal" or "proposed" outcome) — the launcher can act on that to flag
a real problem separately from routine no-op runs, mirroring how
`run_nightly_eval.ps1` treats `perf_check`/`eval_turns` exit codes.
"""
import sys
import time

sys.path.insert(0, __file__.rsplit("\\", 1)[0])
import wes_execute  # noqa: E402
import wes_yahoo  # noqa: E402


def run_all(_teams_fn=None, _propose_fn=None):
    """Run the GM cycle for every propose/auto team. Returns True if every
    team's cycle completed without raising (regardless of what it decided to
    do); False if any team's cycle itself errored."""
    teams_fn = _teams_fn or wes_yahoo.configured_teams
    propose = _propose_fn or wes_execute.propose_lineup_change
    teams = [t for t in teams_fn() if str(t.get("autonomy", "")).lower() in
            ("propose", "auto")]
    if not teams:
        print("[fantasy-gm] no propose/auto teams configured — nothing to do")
        return True

    ok = True
    for team in teams:
        name = team.get("name", "?")
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        try:
            out = propose(name)
        except Exception as e:  # noqa: BLE001 — one team's crash must not
            # stop the rest of the run or crash the scheduled task.
            print(f"[fantasy-gm] {stamp} {name}: ERROR {e!r}", flush=True)
            ok = False
            continue
        marker = "[EXECUTED] " if out.startswith("Set the lineup") else ""
        first_line = out.split("\n", 1)[0]
        print(f"[fantasy-gm] {stamp} {marker}{name}: {first_line}", flush=True)
        if len(out.splitlines()) > 1:
            for line in out.splitlines()[1:]:
                print(f"    {line}", flush=True)
    return ok


if __name__ == "__main__":
    sys.exit(0 if run_all() else 1)
