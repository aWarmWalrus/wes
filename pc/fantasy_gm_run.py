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


def _report(label, name, out):
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    marker = "[EXECUTED] " if out.startswith(("Set the lineup",
                                              "Made this roster move")) else ""
    print(f"[fantasy-gm] {stamp} {marker}{name} ({label}): "
          f"{out.split(chr(10), 1)[0]}", flush=True)
    for line in out.splitlines()[1:]:
        print(f"    {line}", flush=True)


def run_all(_teams_fn=None, _propose_fn=None, _roster_fn=None):
    """Run the GM cycle for every propose/auto team: the LINEUP check, then the
    ROSTER check. Returns True if every team's cycle completed without raising
    (regardless of what it decided to do); False if any team's cycle errored.

    The roster check runs with execute=False — always, regardless of autonomy.
    A drop is irreversible, so the scheduled job may SUGGEST one but never make
    one on its own; the owner asks for it explicitly (#035). Its ledger entry is
    what the Discord bot turns into a DM, and only when the suggestion has
    CHANGED, so a standing recommendation doesn't nag every morning."""
    teams_fn = _teams_fn or wes_yahoo.configured_teams
    propose = _propose_fn or wes_execute.propose_lineup_change
    roster = _roster_fn or wes_execute.propose_roster_moves
    teams = [t for t in teams_fn() if str(t.get("autonomy", "")).lower() in
            ("propose", "auto")]
    if not teams:
        print("[fantasy-gm] no propose/auto teams configured — nothing to do")
        return True

    ok = True
    for team in teams:
        name = team.get("name", "?")
        for label, fn in (("lineup", propose), ("roster", roster)):
            stamp = time.strftime("%Y-%m-%d %H:%M:%S")
            try:
                out = fn(name)
            except Exception as e:  # noqa: BLE001 — one check's crash must not
                # stop the other, the rest of the teams, or the scheduled task.
                print(f"[fantasy-gm] {stamp} {name} ({label}): ERROR {e!r}",
                      flush=True)
                ok = False
                continue
            _report(label, name, out)
    return ok


if __name__ == "__main__":
    sys.exit(0 if run_all() else 1)
