"""NFL fantasy valuation — points-based (tickets #029 P7-pulled-forward, #030).

The NBA half of the engine values players by per-league roto **z-scores**
(`wes_fantasy.rank_by_zscore`): categories, normalized against the league pool.
That is the wrong model for NFL, which is almost always **points-based** — one
number per player, computed by running a stat line through the league's scoring
settings. So NFL gets its own valuer here, behind the SAME interface the
optimizer and the draft recommender already consume:

    rank_*(pool, ...) -> [{**player, "value": float, ...}]  sorted desc

`wes_fantasy.optimize_lineup` and `wes_draft.best_available` only ever read
`value` and `positions`, so they work unchanged against either sport.

DESIGN (same rule as the rest of the engine): pure, deterministic arithmetic
over real numbers. No LLM, no network in this module — the model reads a
formatted line and reasons about it, it never invents a stat. Every scoring
weight is data, so a league with custom settings is a dict change, not a code
change.

STAT-LINE SHAPE: the same `{"name", "cats": {...}}` dicts wes_nba produces, so
the two sports' pools are interchangeable to callers. `cats` keys are Yahoo's
stat names (see _ALIASES for the spellings accepted).

SCOPE: offensive skill positions + K + DEF/ST, i.e. standard redraft roster
slots. IDP (individual defensive players) is not modelled — see NOT_MODELLED.
"""
import re

# --- scoring presets ---------------------------------------------------------
# Yahoo's default NFL scoring. The three presets differ ONLY in Rec (points per
# reception): that single knob is what "standard / half-PPR / full PPR" means,
# and it changes valuation enough that using the wrong one misranks every WR and
# RB. Callers should pass the league's real value once the settings scrape lands.
_BASE = {
    # Passing — Yahoo gives 1 point per 25 passing yards.
    "PassYds": 0.04, "PassTD": 4.0, "Int": -1.0,
    # Rushing / receiving — 1 point per 10 yards.
    "RushYds": 0.1, "RushTD": 6.0,
    "RecYds": 0.1, "RecTD": 6.0, "Rec": 0.0,
    # Misc offence.
    "FumLost": -2.0, "2PT": 2.0, "RetTD": 6.0, "OffFumRetTD": 6.0,
    # Kicking. Yahoo's default pays more for long field goals; a flat "FG" key
    # is accepted too for feeds that don't split by distance.
    "XP": 1.0, "FG": 3.0,
    "FG0_19": 3.0, "FG20_29": 3.0, "FG30_39": 3.0, "FG40_49": 4.0, "FG50": 5.0,
    "XPMiss": -1.0, "FGMiss": -1.0,
    # Team defence / special teams.
    "Sack": 1.0, "DefInt": 2.0, "FumRec": 2.0, "DefTD": 6.0,
    "Safety": 2.0, "BlkKick": 2.0, "DefRetTD": 6.0, "XPReturn": 2.0,
}
SCORING_STANDARD = dict(_BASE, Rec=0.0)
SCORING_HALF_PPR = dict(_BASE, Rec=0.5)
SCORING_PPR = dict(_BASE, Rec=1.0)
_PRESETS = {"standard": SCORING_STANDARD, "std": SCORING_STANDARD,
            "half": SCORING_HALF_PPR, "half_ppr": SCORING_HALF_PPR,
            "ppr": SCORING_PPR, "full_ppr": SCORING_PPR}
DEFAULT_SCORING = SCORING_HALF_PPR   # the most common modern default

# Points allowed by a team defence -> points, as (max_allowed, points) tiers,
# checked in order. Yahoo's default ladder. Kept separate from the linear
# weights above because it is a step function, not a per-unit rate.
POINTS_ALLOWED_TIERS = [(0, 10.0), (6, 7.0), (13, 4.0), (20, 1.0),
                        (27, 0.0), (34, -1.0), (10**6, -4.0)]

# Stat names that mean the same thing, so a feed's spelling doesn't silently
# score as zero. Maps alias -> canonical key used in the tables above.
_ALIASES = {
    "PASSINGYARDS": "PassYds", "PASSYARDS": "PassYds", "PYDS": "PassYds",
    "PASSINGTOUCHDOWNS": "PassTD", "PASSTD": "PassTD", "PTD": "PassTD",
    "INTERCEPTIONS": "Int", "INT": "Int", "INTS": "Int",
    "RUSHINGYARDS": "RushYds", "RUSHYARDS": "RushYds", "RUYDS": "RushYds",
    "RUSHINGTOUCHDOWNS": "RushTD", "RUSHTD": "RushTD",
    "RECEIVINGYARDS": "RecYds", "RECYARDS": "RecYds", "REYDS": "RecYds",
    "RECEIVINGTOUCHDOWNS": "RecTD", "RECTD": "RecTD",
    "RECEPTIONS": "Rec", "REC": "Rec", "CATCHES": "Rec",
    "FUMBLESLOST": "FumLost", "FUML": "FumLost", "FUM": "FumLost",
    "TWOPOINTCONVERSIONS": "2PT", "2PTCONV": "2PT",
    "RETURNTOUCHDOWNS": "RetTD", "RETTD": "RetTD",
    "EXTRAPOINTSMADE": "XP", "XPMADE": "XP", "PAT": "XP",
    "FIELDGOALSMADE": "FG", "FGMADE": "FG",
    "SACKS": "Sack", "SACK": "Sack",
    "DEFENSIVEINTERCEPTIONS": "DefInt", "DEFINT": "DefInt",
    "FUMBLESRECOVERED": "FumRec", "FUMREC": "FumRec",
    "DEFENSIVETOUCHDOWNS": "DefTD", "DEFTD": "DefTD",
    "SAFETIES": "Safety", "BLOCKEDKICKS": "BlkKick",
    "POINTSALLOWED": "PtsAllowed", "PTSALLOWED": "PtsAllowed", "PA": "PtsAllowed",
}

# Deliberately out of scope, recorded so a future reader doesn't assume a bug:
NOT_MODELLED = (
    "IDP (individual defensive players: DL/LB/DB slots) — Yahoo's default "
    "redraft leagues use a single team DEF slot, and the optimizer's NFL table "
    "has no IDP slots either. Add both together if a league needs them.",
    "Bonus thresholds (e.g. +3 for a 300-yard passing game) and decimal-yardage "
    "leagues — both are extra scoring keys, not new logic.",
    "Projections. This scores a stat line that already exists. Valuing a player "
    "for a FUTURE week needs projections, which are a data-source problem "
    "(#030's ESPN NFL pool fetch), not a scoring one.",
)


# --- reading a league's REAL scoring off Yahoo's settings page ---------------
# The presets above are educated defaults; this parses what the league actually
# uses, which is what makes valuation mean "value in THIS league" (design §1).
# Verified against nfl.l.957011 on 2026-07-29 — its settings matched
# SCORING_HALF_PPR exactly, so the presets were right, but assuming that for
# every league would misrank every RB/WR the moment one differs.
#
# SECTION AWARENESS IS MANDATORY, not tidiness: Yahoo reuses labels across
# sections with OPPOSITE meanings. "Interceptions -1" under Offense is a QB
# throwing one; "Interception 2" under Defense/Special Teams is a defence
# catching one. A flat label->key map silently scores one as the other.
_SECTION_MARK = "league value"          # section headers end "League Value ..."
_SECTIONS = ("offense", "kickers", "defense")
_SECTION_ALIASES = {"offense": "offense", "kickers": "kickers",
                    "kicking": "kickers", "defense/special teams": "defense",
                    "defense": "defense", "special teams": "defense"}

# (section, exact label) -> canonical stat key. Matched longest-label-first.
_SETTING_LABELS = {
    "offense": {
        "Passing Yards": "PassYds", "Passing Touchdowns": "PassTD",
        "Interceptions": "Int",
        "Rushing Yards": "RushYds", "Rushing Touchdowns": "RushTD",
        "Receptions": "Rec",
        "Receiving Yards": "RecYds", "Receiving Touchdowns": "RecTD",
        "Return Touchdowns": "RetTD", "2-Point Conversions": "2PT",
        "Fumbles Lost": "FumLost", "Offensive Fumble Return TD": "OffFumRetTD",
    },
    "kickers": {
        "Field Goals 0-19 Yards": "FG0_19", "Field Goals 20-29 Yards": "FG20_29",
        "Field Goals 30-39 Yards": "FG30_39", "Field Goals 40-49 Yards": "FG40_49",
        "Field Goals 50+ Yards": "FG50",
        "Point After Attempt Made": "XP", "Point After Attempt Missed": "XPMiss",
    },
    "defense": {
        "Sack": "Sack", "Interception": "DefInt", "Fumble Recovery": "FumRec",
        "Touchdown": "DefTD", "Safety": "Safety", "Block Kick": "BlkKick",
        "Kickoff and Punt Return Touchdowns": "DefRetTD",
        "Extra Point Returned": "XPReturn",
    },
}
# "25 yards per point" means 1/25 of a point per yard — a RATE, not a weight.
_PER_POINT_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*yards?\s*per\s*point$", re.I)
_PLAIN_RE = re.compile(r"^(-?\d+(?:\.\d+)?)$")
# "Points Allowed 1-6 points 7" / "0 points 10" / "35+ points -4"
_PA_RE = re.compile(
    r"^(\d+)(?:\s*-\s*(\d+))?(\+)?\s*points?\s+(-?\d+(?:\.\d+)?)$", re.I)
_ROSTER_RE = re.compile(r"^Roster Positions:\s*(.+)$", re.I)


def _section_of(line):
    """The section a header line declares, or None if it isn't a header."""
    low = line.lower()
    if _SECTION_MARK not in low:
        return None
    head = low.split(_SECTION_MARK)[0].strip()
    return _SECTION_ALIASES.get(head, "offense" if not head else None)


def _parse_value(text):
    """A settings value -> a per-unit weight, or None if it isn't one."""
    m = _PER_POINT_RE.match(text)
    if m:
        per = float(m.group(1))
        return round(1.0 / per, 6) if per else None
    m = _PLAIN_RE.match(text)
    return float(m.group(1)) if m else None


def parse_scoring(lines):
    """Yahoo settings-page lines -> this league's real scoring.

    Returns {"weights": {...}, "tiers": [(max_allowed, points), ...],
             "unknown": [lines that looked like settings but didn't match]}.
    Pure — takes text, no browser. `unknown` exists so scraper drift shows up as
    data instead of silently scoring a category as zero.

    Weights merge onto DEFAULT_SCORING, so a league that omits a stat still
    scores it sanely rather than at 0."""
    weights, tiers, unknown = {}, [], []
    section = None
    for raw in (lines or []):
        line = " ".join(str(raw).split())
        if not line:
            continue
        sec = _section_of(line)
        if sec:
            section = sec
            continue
        if section is None:
            continue                      # still in the general-settings table
        if line.lower().startswith("points allowed"):
            m = _PA_RE.match(line[len("points allowed"):].strip())
            if m:
                lo, hi, plus, val = m.groups()
                ceiling = 10**6 if plus else int(hi or lo)
                tiers.append((ceiling, float(val)))
            else:
                unknown.append(line)
            continue
        labels = _SETTING_LABELS.get(section, {})
        for label in sorted(labels, key=len, reverse=True):
            if line.startswith(label):
                val = _parse_value(line[len(label):].strip())
                if val is None:
                    unknown.append(line)
                else:
                    weights[labels[label]] = val
                break
    merged = dict(DEFAULT_SCORING, **weights)
    return {"weights": merged, "parsed": weights,
            "tiers": sorted(tiers) or list(POINTS_ALLOWED_TIERS),
            "unknown": unknown}


def parse_roster_slots(lines):
    """The league's roster slots from 'Roster Positions: QB, WR, WR, ...'.

    Authoritative league structure, straight from settings — unlike deriving
    slots from a scraped roster, this works before a roster exists (pre-draft)
    and includes empty slots. Feeds wes_fantasy.optimize_lineup directly."""
    for raw in (lines or []):
        m = _ROSTER_RE.match(" ".join(str(raw).split()))
        if m:
            return [p.strip() for p in m.group(1).split(",") if p.strip()]
    return []


def _canon(cats):
    """Normalize a stat dict's keys to the canonical names, so 'receptions',
    'Rec' and 'REC' all score. Unknown keys are kept as-is (they simply find no
    weight and contribute nothing, rather than raising)."""
    out = {}
    for k, v in (cats or {}).items():
        key = _ALIASES.get(str(k).replace(" ", "").replace("_", "").upper(), k)
        out[key] = v
    return out


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def scoring_preset(name):
    """A scoring dict by name ('standard' | 'half' | 'ppr'), else the default.
    Accepts None so callers can pass a league setting through untouched."""
    return _PRESETS.get(str(name or "").strip().lower().replace("-", "_"),
                        DEFAULT_SCORING)


def points_allowed_value(pts, tiers=None):
    """Points for a defence that allowed `pts` points, via the tier ladder.
    `tiers` lets a league's real ladder (from parse_scoring) override the default."""
    ladder = tiers or POINTS_ALLOWED_TIERS
    if not _num(pts):
        return 0.0
    for ceiling, val in ladder:
        if pts <= ceiling:
            return val
    return ladder[-1][1]


def fantasy_points(cats, scoring=None, tiers=None):
    """Fantasy points for one stat line under `scoring` (a weights dict, or a
    preset name, or None for the default). Pure.

    Whatever period the stat line covers is the period the answer covers — pass
    per-game averages and you get per-game points; pass season totals and you
    get season points. The caller decides; this does arithmetic.

    Unknown stats score nothing and unscored stats are skipped, so a partial
    feed degrades to a smaller number instead of raising."""
    weights = scoring if isinstance(scoring, dict) else scoring_preset(scoring)
    stats = _canon(cats)
    total = 0.0
    for key, val in stats.items():
        if key == "PtsAllowed":            # step function, handled below
            continue
        w = weights.get(key)
        if w is not None and _num(val):
            total += w * val
    if "PtsAllowed" in stats:
        total += points_allowed_value(stats["PtsAllowed"], tiers)
    return round(total, 2)


def rank_by_points(pool, scoring=None, tiers=None):
    """Rank an NFL player pool by fantasy points (highest first). Each returned
    dict is the input player plus `value` (its fantasy points). Mirrors
    `wes_fantasy.rank_by_zscore`'s contract so the optimizer and the draft
    recommender consume either sport identically.

    NOTE the deliberate asymmetry with the NBA valuer: a z-score is *relative*
    to the pool passed in, so ranking two players there is meaningless. Points
    are ABSOLUTE, so this is meaningful for any number of players — including
    one. That is a property of the scoring model, not an oversight."""
    ranked = [{**p, "value": fantasy_points(p.get("cats"), scoring, tiers)}
              for p in (pool or [])]
    ranked.sort(key=lambda x: x["value"], reverse=True)
    return ranked


def format_points(player, scoring=None):
    """One compact line for the model to relay: name, position, points, and the
    biggest contributors. Degrades to a string, never raises into a turn."""
    if not isinstance(player, dict):
        return "No NFL stat line available."
    cats = _canon(player.get("cats"))
    weights = scoring if isinstance(scoring, dict) else scoring_preset(scoring)
    pts = fantasy_points(player.get("cats"), scoring)
    parts = sorted(
        ((k, weights.get(k, 0.0) * v) for k, v in cats.items()
         if _num(v) and weights.get(k)),
        key=lambda kv: abs(kv[1]), reverse=True)[:4]
    tail = ", ".join(f"{k} {v:+g}" for k, v in parts)
    pos = "/".join(player.get("positions") or []) or "?"
    name = player.get("name", "Unknown")
    return f"{name} ({pos}): {pts:g} fantasy pts" + (f" — {tail}" if tail else "")
