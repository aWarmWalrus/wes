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
import urllib.parse

import wes_fantasy  # noqa: E402 — same dir on path (server/tests add it)
import wes_http  # noqa: E402 — raw data layer (#034)
import wes_nba  # noqa: E402

# ESPN's free bulk season-stats endpoint: one page returns many athletes, each
# with `athlete` (id/name/position/team) and `categories` (positional value
# arrays whose labels come from the top-level `categories[].names`).
_BYATHLETE = ("https://site.web.api.espn.com/apis/common/v3/sports/basketball/"
              "nba/statistics/byathlete")

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
    """ESPN JSON via the shared raw layer (#034). SEASON_TTL — a season's stat
    totals don't change, and this pool fetch was previously uncached."""
    return wes_http.get_json(url, ttl=wes_http.SEASON_TTL, timeout=15.0)


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


# --- snake draft position math (#039, Sleeper) -------------------------------
# PURE. No network, no clock — the arithmetic that answers "whose pick is this"
# and "when do I pick next", which a draft assistant is useless without.
def slot_for_pick(pick_no, teams, reversal_round=0):
    """Which draft SLOT (1..teams) owns overall pick `pick_no` (1-indexed).

    Snake: odd rounds run 1->N, even rounds run N->1. `reversal_round` is
    Sleeper's third-round-reversal option (0 = off); when set, the snake flips
    an extra time from that round on, so rounds >= it invert the usual parity.
    """
    if teams <= 0 or pick_no <= 0:
        return None
    rnd = (pick_no - 1) // teams + 1
    idx = (pick_no - 1) % teams
    forward = (rnd % 2 == 1)
    if reversal_round and rnd >= reversal_round:
        forward = not forward
    return idx + 1 if forward else teams - idx


def next_pick_for_slot(slot, teams, picks_made, rounds, reversal_round=0):
    """The next overall pick number belonging to `slot`, or None if their draft
    is done. `picks_made` is how many picks have already happened."""
    for pick_no in range(picks_made + 1, teams * rounds + 1):
        if slot_for_pick(pick_no, teams, reversal_round) == slot:
            return pick_no
    return None


def picks_until_turn(slot, teams, picks_made, rounds, reversal_round=0):
    """How many picks until `slot` is on the clock (0 = on the clock now).

    This is the number that decides strategy: with 20 picks to wait you can let
    a run happen, with 1 you cannot."""
    nxt = next_pick_for_slot(slot, teams, picks_made, rounds, reversal_round)
    return None if nxt is None else nxt - picks_made - 1


_FLEX_MEMBERS = {
    "W/R/T": ("RB", "WR", "TE"), "W/R": ("RB", "WR"),
    "Q/W/R/T": ("QB", "RB", "WR", "TE"),
}


def targets_from_slots(slots):
    """League roster slots -> (dedicated targets, flex count, flex positions).

    PURE, and DERIVED from the league's own configuration rather than
    hardcoded: a superflex or 3-WR league wants a different shape, and a fixed
    table would quietly give bad advice in one.

    **Flex slots are deliberately NOT folded into the per-position targets.**
    Adding each FLEX to every position it accepts looked reasonable and produced
    `TE: 3` for a standard 2-flex league — nobody starts three tight ends, and a
    need bump built on that would keep recommending one. Flex is real capacity
    but it is capacity for ONE of several positions, not for each of them, so it
    is returned separately and applied as a weaker, shared bump."""
    targets, flex, flex_pos = {}, 0, set()
    for slot in slots or ():
        s = str(slot).upper()
        if s in ("BN", "IR", "TAXI"):
            continue
        if s in _FLEX_MEMBERS:
            flex += 1
            flex_pos |= set(_FLEX_MEMBERS[s])
        else:
            targets[s] = targets.get(s, 0) + 1
    return targets, flex, flex_pos


def flex_share(flex, flex_pos):
    """Flex capacity attributable to ONE position. PURE.

    Flex is capacity for one of SEVERAL positions, not capacity for each of
    them, so it is split evenly across the eligible ones. Crude -- it does not
    pretend to know this league's flex habits -- but it moves the number in the
    right direction and, more importantly, it is ONE number used everywhere.

    Extracted 2026-09-04 because it was not. `replacement_levels` divided the
    flex; the roster-need penalty in the Sleeper board added it WHOLE to every
    eligible position, so a 2-flex league tolerated 5 RBs, 5 WRs and 4 TEs
    before any over-fill cost -- fourteen slots of tolerance on a fifteen-man
    roster. The penalty therefore almost never bound, and once RB and WR were
    nominally filled, raw VOR decided every remaining pick. `targets_from_slots`
    already refuses to do exactly this (see its docstring, on TE: 3); the
    penalty was the one place that still did.
    """
    return (flex / len(flex_pos)) if flex and flex_pos else 0.0


def replacement_levels(players, targets, flex, flex_pos, teams):
    """Per-position replacement value: what the LAST startable player at that
    position is worth, league-wide. PURE.

    Why this exists, concretely: ranking a draft board by raw fantasy points put
    six quarterbacks in the top eight (2026-08-14, real board). QBs out-score
    running backs in most systems — but a 12-team league starts 12 QBs and ~36
    RB/WR, so the 12th-best QB is nearly as good as the best, while the 36th
    receiver is far worse than the 5th. What a pick is WORTH is the gap to the
    player you could have had anyway, not the raw total.

      players: [{positions, value}] — the whole available pool
      targets: dedicated starting slots per position (targets_from_slots)
      flex/flex_pos: shared flex capacity, spread across eligible positions

    Returns {position: replacement_value}. A position nobody starts gets 0.0,
    which correctly makes every player at it pure surplus rather than crashing.
    """
    by_pos = {}
    for p in players:
        pos = (p.get("positions") or [None])[0]
        if pos and p.get("value") is not None:
            by_pos.setdefault(pos, []).append(float(p["value"]))
    for vals in by_pos.values():
        vals.sort(reverse=True)

    share = flex_share(flex, flex_pos)

    out = {}
    for pos, vals in by_pos.items():
        starters = targets.get(pos, 0) + (share if pos in flex_pos else 0.0)
        rank = int(round(teams * starters))
        if rank <= 0:
            out[pos] = 0.0                 # nobody starts one: all surplus
        elif rank <= len(vals):
            out[pos] = vals[rank - 1]
        else:
            out[pos] = vals[-1]            # shallower pool than slots
    return out


def must_fill(unfilled, picks_left):
    """Positions the roster can no longer AFFORD to skip, or () if none.

    A soft need bump cannot finish a roster. K and DEF carry `urgency` 0.25
    precisely so nobody drafts a kicker in round 7, and replacement level at
    those positions is nearly flat -- so their VOR is low BY CONSTRUCTION and
    a skill player always scores better. The model duly took a running back
    with its last pick and finished a 15-round draft with no kicker and no
    defense, saying so in its own reasoning: "a high-upside WR is preferable
    over a kicker" (2026-08-22, full mock, 15 of 15 picks made).

    No prompt fixes that, because on value the model is RIGHT every time. The
    engine has to stop offering the choice: once the picks remaining are all
    spoken for by slots still empty, this pick MUST fill one of them.

    `picks_left` counts THIS pick. Returns the positions still unfilled when
    there is no slack left, else () -- caller keeps the full board.
    """
    gaps = {pos: n for pos, n in (unfilled or {}).items() if n > 0}
    if not gaps or picks_left is None:
        return ()
    return tuple(sorted(gaps)) if picks_left <= sum(gaps.values()) else ()


# --- roster construction constraints (#039) ---------------------------------
# PURE. Value over replacement says who is BEST; these say who FITS. A board
# that only knows value will happily hand you three Bengals and four players on
# the week-9 bye, and be arithmetically correct while losing you the week.
#
# Owner, 2026-08-15, arguing against a pre-ranked queue: "there is some decision
# making around team fit, making sure bye weeks are staggered, not having too
# many players from same team". A queue fixed in advance cannot express any of
# that, because it cannot know what fell to you.
def _ordinal(n):
    """1 -> '1st'. Teens are the exception that catches naive implementations
    (11th, not 11st)."""
    if 10 <= n % 100 <= 20:
        return f"{n}th"
    return f"{n}{ {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th') }".replace(" ", "")


DEFAULT_MAX_PER_NFL_TEAM = 3
BYE_PENALTY_PER_EXTRA = 1.25
BYE_FREE_ALLOWANCE = 2


def roster_fit(candidate, my_players, byes, max_per_team=DEFAULT_MAX_PER_NFL_TEAM,
               bye_free=BYE_FREE_ALLOWANCE,
               bye_penalty=BYE_PENALTY_PER_EXTRA):
    """(allowed, penalty, reasons) for adding `candidate` to `my_players`.

    Two different KINDS of rule, deliberately kept apart:

    * **Same-team stacking is a hard cap.** It is countable, so it is a
      constraint rather than a judgement (#038's distinction) — at the cap the
      candidate is excluded outright, not merely discouraged, because "one more
      Bengal" is never a matter of degree once the correlation risk is taken.
    * **Bye clustering is a soft penalty.** Some overlap is unavoidable and
      harmless; the harm scales with how many players already share the week.
      A hard rule here would refuse good players for a cost that is real but
      small.

    Penalties are in the same units as value-over-replacement, so they trade off
    against it honestly, and every one comes back with a REASON so the
    recommendation can explain itself rather than just reordering silently.

    An unknown bye contributes nothing — the honest reading of missing data, and
    the alternative (treating it as some default week) would cluster a roster
    onto a week nobody is actually on bye."""
    reasons = []
    team = (candidate.get("team") or "").strip()
    pos = (candidate.get("positions") or [None])[0]

    same_team = sum(1 for p in my_players
                    if (p.get("team") or "").strip() == team and team)
    if team and same_team >= max_per_team:
        return False, 0.0, [f"already have {same_team} from {team}"]

    penalty = 0.0
    if team and same_team == max_per_team - 1:
        penalty += 0.5
        reasons.append(f"would be your {_ordinal(same_team + 1)} from {team}")

    bye = byes.get(team) if team else None
    if bye is not None:
        shared = sum(1 for p in my_players
                     if byes.get((p.get("team") or "").strip()) == bye)
        if shared >= bye_free:
            extra = shared - bye_free + 1
            penalty += bye_penalty * extra
            reasons.append(f"{shared + 1} players on the week-{bye} bye")
    return True, round(penalty, 2), reasons
