"""Fantasy draft engine (ticket #030).

The DECISION half of an autonomous draft: value the whole draftable player pool
against a league's scoring (real z-scores), then recommend the next pick given
who's already gone and the roster built so far. This is pure/deterministic code
over real stats — the same design as the rest of the fantasy engine (#029 §8.2):
arithmetic is code, the model only relays the result.

SCOPE: this is the engine ONLY. The live Yahoo draft-room automation — reading
the live board (who's on the clock, who's picked, the timer) and submitting a
pick on the clock via scripted Playwright — is the separate, harder half and is
NOT here yet (it needs a recon session against a real draft-room DOM). Everything
in this file runs offline against ESPN season stats, no Yahoo.

DATA: the population fetch #029 kept deferring. ESPN's `byathlete` bulk stats
endpoint returns hundreds of players' season lines (plus position) in one call,
so we can normalize each category by the real pool — no per-player fan-out.

Every entry point degrades to a safe value (`[]` / a string), never raises.
"""
import json
import urllib.parse
import urllib.request

import wes_fantasy  # noqa: E402 — same dir on path (server/tests add it)
import wes_nba  # noqa: E402

# ESPN's free bulk season-stats endpoint: one page returns many athletes, each
# with `athlete` (id/name/position/team) and `categories` (positional value
# arrays whose labels come from the top-level `categories[].names`).
_BYATHLETE = ("https://site.web.api.espn.com/apis/common/v3/sports/basketball/"
              "nba/statistics/byathlete")
_UA = {"User-Agent": "Mozilla/5.0 (WES draft engine)"}

# Fantasy category (as Yahoo scores it) -> (ESPN category group, ESPN label).
# Mirrors wes_nba._CAT_SOURCES but reads the BULK endpoint's per-game fields.
_ESPN_LABEL = {
    "PTS": ("offensive", "avgPoints"), "REB": ("general", "avgRebounds"),
    "AST": ("offensive", "avgAssists"), "ST": ("defensive", "avgSteals"),
    "STL": ("defensive", "avgSteals"), "BLK": ("defensive", "avgBlocks"),
    "TO": ("offensive", "avgTurnovers"), "TOV": ("offensive", "avgTurnovers"),
    "3PM": ("offensive", "avgThreePointFieldGoalsMade"),
    "FG%": ("offensive", "fieldGoalPct"), "FT%": ("offensive", "freeThrowPct"),
    "3P%": ("offensive", "threePointFieldGoalPct"),
    "DD": ("general", "doubleDouble"), "TD": ("general", "tripleDouble"),
    "EJCT": ("general", "ejections"),
}
_COUNTING = {"DD", "TD", "EJCT"}  # season totals, not per-game averages

# ESPN gives a COARSE position (Guard/Forward/Center -> G/F/C); Yahoo splits
# G into PG/SG and F into SF/PF, but the bulk feed doesn't. Coarse is a fine
# v1 for draft roster-need (a "G" fills any guard slot). Standard roster targets
# by coarse bucket (starters + typical bench depth) drive the positional bump.
_COARSE_TARGET = {"G": 5, "F": 5, "C": 3}


def _get_json(url):
    req = urllib.request.Request(url, headers=_UA)
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode())


# --- pool ingestion (the population fetch) ----------------------------------

def parse_byathlete(payload):
    """ESPN `byathlete` JSON -> list of stat-line dicts shaped like
    wes_nba.parse_season_stats (name, season, gp, min, team, positions, cats,
    counting). The value arrays are positional; their labels live in the
    top-level `categories[].names`, so we zip them per group. Pure."""
    if not isinstance(payload, dict):
        return []
    season = str((payload.get("requestedSeason") or {}).get("displayName", ""))
    labels = {c.get("name"): (c.get("names") or [])
              for c in payload.get("categories", [])}
    out = []
    for a in payload.get("athletes", []):
        ath = a.get("athlete", {}) or {}
        by_group = {}
        for cat in a.get("categories", []):
            names = labels.get(cat.get("name"), [])
            by_group[cat.get("name")] = dict(zip(names, cat.get("values") or []))

        def get(group, label, _bg=by_group):
            return _bg.get(group, {}).get(label)

        cats = {}
        for fc, (grp, lbl) in _ESPN_LABEL.items():
            v = get(grp, lbl)
            if v is not None:
                cats[fc] = int(v) if fc in _COUNTING else round(float(v), 3)
        pos = (ath.get("position") or {}).get("abbreviation")
        name = ath.get("displayName", "")
        if not name:
            continue
        out.append({
            "name": name,
            "season": season,
            "gp": get("general", "gamesPlayed"),
            "min": get("general", "avgMinutes"),
            "team": ath.get("teamName", ""),
            "positions": [pos] if pos else [],
            "cats": cats,
            "counting": _COUNTING & set(cats),
        })
    return out


def draftable_pool(limit=180, _get_fn=None):
    """The draftable population: the top `limit` NBA players' season stat lines,
    for computing real z-scores. Live/network — degrades to `[]` on any failure
    so a bad ESPN response never crashes a turn. `_get_fn` injectable for tests.

    Sorted by scoring as a rough "worth rostering" proxy; a minutes-based or
    larger pool is a refinement (a top-scorers pool slightly inflates the
    baselines, but the RANKING within it is unaffected)."""
    get = _get_fn or _get_json
    url = _BYATHLETE + "?" + urllib.parse.urlencode({
        "region": "us", "lang": "en", "contentorigin": "espn",
        "limit": min(int(limit), 200), "sort": "offensive.avgPoints:desc"})
    try:
        return parse_byathlete(get(url))
    except Exception:  # noqa: BLE001
        return []


# --- pick recommendation ----------------------------------------------------

def best_available(ranked, drafted=(), my_roster=(), need_weight=1.0, limit=10):
    """Recommend the next pick from a z-score-`ranked` pool (wes_fantasy.
    rank_by_zscore output). Drops everyone already taken and applies a
    positional-need bump so a roster thin at a position isn't handed yet another
    player it can't slot. Pure.

      ranked:      pool with `value` (z-total) + `positions`, best first
      drafted:     names taken by ANYONE (removes them from the board)
      my_roster:   the stat-line dicts I already hold (drives positional need)
      need_weight: how hard to weight roster need vs raw value (0 = pure BPA)

    Returns the available players (best first by need-adjusted value), each with
    `need_bump` + `adj_value` added, capped at `limit`."""
    taken = {wes_nba._norm(n) for n in drafted}
    taken |= {wes_nba._norm(p.get("name", "")) for p in my_roster}
    have = {}
    for p in my_roster:
        pos = (p.get("positions") or [None])[0]
        if pos:
            have[pos] = have.get(pos, 0) + 1

    out = []
    for p in ranked:
        if wes_nba._norm(p.get("name", "")) in taken:
            continue
        pos = (p.get("positions") or [None])[0]
        target = _COARSE_TARGET.get(pos, 0)
        gap = max(0, target - have.get(pos, 0)) if target else 0
        # bump scales with how UNDER-filled the position is (0 when full/unknown)
        bump = need_weight * gap / target if target else 0.0
        out.append({**p, "need_bump": round(bump, 2),
                    "adj_value": round(p.get("value", 0.0) + bump, 2)})
    out.sort(key=lambda x: x["adj_value"], reverse=True)
    return out[:limit]


def format_board(recs, n=10):
    """Compact top-N recommendation list for the model to relay."""
    if not recs:
        return "No players available to recommend."
    lines = ["Best available:"]
    for i, p in enumerate(recs[:n], 1):
        pos = "/".join(p.get("positions") or []) or "?"
        lines.append(f"{i}. {p['name']} ({pos}) — value {p.get('adj_value', 0):g}")
    return "\n".join(lines)


def recommend_pick(categories=None, drafted=(), my_roster=(), limit=10,
                   _pool_fn=None):
    """End-to-end (fetch pool -> z-score rank -> best available) for a live draft
    aid. ADVISE only. Degrades to a string on any problem. `_pool_fn` injectable
    for tests. `categories` defaults to the standard roto set."""
    pool = (_pool_fn or draftable_pool)()
    if not pool:
        return "I couldn't fetch the player pool just now."
    cats = categories or wes_fantasy.DEFAULT_CATEGORIES
    ranked = wes_fantasy.rank_by_zscore(pool, cats)
    recs = best_available(ranked, drafted=drafted, my_roster=my_roster,
                          limit=limit)
    return format_board(recs, n=limit)
