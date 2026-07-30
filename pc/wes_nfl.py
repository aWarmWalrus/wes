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
    "FumLost": -2.0, "2PT": 2.0, "RetTD": 6.0,
    # Kicking. Yahoo's default pays more for long field goals; a flat "FG" key
    # is accepted too for feeds that don't split by distance.
    "XP": 1.0, "FG": 3.0,
    "FG0_19": 3.0, "FG20_29": 3.0, "FG30_39": 3.0, "FG40_49": 4.0, "FG50": 5.0,
    "XPMiss": -1.0, "FGMiss": -1.0,
    # Team defence / special teams.
    "Sack": 1.0, "DefInt": 2.0, "FumRec": 2.0, "DefTD": 6.0,
    "Safety": 2.0, "BlkKick": 2.0,
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


def points_allowed_value(pts):
    """Points for a defence that allowed `pts` points, via the tier ladder."""
    if not _num(pts):
        return 0.0
    for ceiling, val in POINTS_ALLOWED_TIERS:
        if pts <= ceiling:
            return val
    return POINTS_ALLOWED_TIERS[-1][1]


def fantasy_points(cats, scoring=None):
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
        total += points_allowed_value(stats["PtsAllowed"])
    return round(total, 2)


def rank_by_points(pool, scoring=None):
    """Rank an NFL player pool by fantasy points (highest first). Each returned
    dict is the input player plus `value` (its fantasy points). Mirrors
    `wes_fantasy.rank_by_zscore`'s contract so the optimizer and the draft
    recommender consume either sport identically.

    NOTE the deliberate asymmetry with the NBA valuer: a z-score is *relative*
    to the pool passed in, so ranking two players there is meaningless. Points
    are ABSOLUTE, so this is meaningful for any number of players — including
    one. That is a property of the scoring model, not an oversight."""
    ranked = [{**p, "value": fantasy_points(p.get("cats"), scoring)}
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
