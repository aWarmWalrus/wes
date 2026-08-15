"""NFL schedule -> bye weeks (ticket #039).

Exists because **no fantasy platform we read hands us a bye week.** Sleeper's
player object has 40-odd fields and none of them is the bye; Yahoo shows it on
the page but only for rostered players. A bye is a property of a TEAM'S
SCHEDULE, so it is derived from the schedule: the regular-season week in which a
team has no game.

Source is nflverse's `schedules` release (one ~3.7MB CSV for every season since
1999), chosen for the same reasons as #036 — a handful of HTTP requests instead
of hundreds, and no rate-limit exposure. Stdlib `csv` only; no new dependency.

Layering (docs/data-architecture.md): raw + fantasy-data. The parser below is
PURE and takes already-fetched rows.
"""
import csv
import io
import time

import wes_http

GAMES_URL = ("https://github.com/nflverse/nflverse-data/releases/download/"
             "schedules/games.csv")

# A schedule changes at most a few times a season (flexed games never move a
# bye), so this is deliberately long-lived.
SCHEDULE_TTL = float(24 * 3600)

REGULAR_SEASON_WEEKS = range(1, 19)

_cache = {"at": 0.0, "season": None, "byes": None}


def parse_byes(rows, season, weeks=REGULAR_SEASON_WEEKS):
    """Schedule rows -> {team: bye_week}. PURE.

    A bye is the regular-season week a team does NOT appear in. Derived rather
    than looked up, so it stays correct when the league changes the number of
    weeks — as it did going to 17 games, and would again at 18.

    Only REG games count: preseason and playoffs would both create phantom
    'byes' for teams that simply are not playing then.

    A team with no missing week (or more than one) is omitted rather than
    guessed at. Fantasy code treats a missing bye as UNKNOWN, which is the
    honest answer; inventing week 0 would look like 'never on bye'."""
    played = {}
    for r in rows:
        if str(r.get("season")) != str(season):
            continue
        if str(r.get("game_type") or "").upper() != "REG":
            continue
        try:
            wk = int(r.get("week"))
        except (TypeError, ValueError):
            continue
        for side in ("home_team", "away_team"):
            team = (r.get(side) or "").strip()
            if team:
                played.setdefault(team, set()).add(wk)

    byes = {}
    allw = set(weeks)
    for team, wks in played.items():
        missing = sorted(allw - wks)
        if len(missing) == 1:
            byes[team] = missing[0]
    return byes


def bye_weeks(season, _get_fn=None, _now=None):
    """{team: bye_week} for `season`. Degrades to {} — a missing bye must read
    as UNKNOWN, never as 'no bye', or a draft board would happily stack a whole
    roster onto one week believing it had staggered them."""
    now = _now if _now is not None else time.time()
    if (_cache["byes"] is not None and _cache["season"] == str(season)
            and now - _cache["at"] < SCHEDULE_TTL):
        return _cache["byes"]
    try:
        text = (_get_fn or wes_http.get_text)(GAMES_URL, ttl=SCHEDULE_TTL,
                                              timeout=60)
        rows = list(csv.DictReader(io.StringIO(text)))
        byes = parse_byes(rows, season)
    except Exception:  # noqa: BLE001 — advisory input; never break a draft turn
        return {}
    _cache.update(at=now, season=str(season), byes=byes)
    return byes
