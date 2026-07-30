"""Fantasy valuation / decision engine (ticket #029, P1).

The ingestion ADAPTERS (`wes_yahoo` = league state, `wes_nba` = player stats)
feed structured data here; this module maps a player's real stat line through a
league's scoring so "value" means value to THAT team, not generic goodness. P1 =
`player_value` (a stat line in the league's categories, with an optional
head-to-head compare). The daily lineup optimizer (P2) will live here too.

DESIGN: valuation is deterministic code over real numbers — the LLM reads the
formatted line and reasons ("more points but more turnovers"), it never invents a
stat. Every entry point degrades to a string, never raises into a turn.

P1 SCOPE (deliberate): this shows the category line, not a z-score / replacement
ranking. A z-score needs the whole league player pool (a population fetch) and
belongs with the optimizer (P2). The line + this league's categories is enough to
answer "is X worth starting over Y" from real stats, no guesses.
"""
import statistics

import wes_nba  # noqa: E402 — same dir on path (added by the server/tests)
import wes_nfl  # noqa: E402 — NFL points valuation (sport-specific, pure)
import wes_yahoo  # noqa: E402

# The owner's league (roto) categories, used when the live scoring scrape isn't
# available. TO is a NEGATIVE category (fewer is better) in every roto league.
DEFAULT_CATEGORIES = ["PTS", "REB", "AST", "ST", "BLK", "TO", "DD", "TD", "EJCT"]
_cat_cache = {}  # league_key -> [categories]


def _fmt_cat(stats, cat):
    v = stats["cats"].get(cat)
    if v is None:
        return f"{cat} —"  # league scores this cat but ESPN didn't expose it
    if cat in stats.get("counting", ()):
        return f"{cat} {v}"          # season total (DD/TD/EJCT)
    return f"{cat} {v:g}"            # per-game average


def format_value(stats, categories):
    """One compact line: name, season context, then each league category."""
    cats = categories or DEFAULT_CATEGORIES
    line = " · ".join(_fmt_cat(stats, c) for c in cats)
    gp, mn = stats.get("gp"), stats.get("min")
    ctx = f"{stats.get('season', '?')}, {gp} GP" + (f", {mn:g} MPG" if mn else "")
    return f"{stats['name']} ({ctx}): {line}"


def player_value(player, categories=None, versus=None, _stats_fn=None):
    """Value line for `player` (and optional `versus`) in a league's categories.
    Returns a compact string for the model, or a degradation string on failure.
    Per-game except DD/TD/EJCT (season totals); TO is negative (lower better)."""
    fetch = _stats_fn or wes_nba.player_season_stats
    a = fetch(player)
    if not isinstance(a, dict):
        return a  # a degradation string from the stats layer
    lines = [format_value(a, categories)]
    if versus:
        b = fetch(versus)
        if not isinstance(b, dict):
            return b
        lines.append(format_value(b, categories))
        lines.append("(Per-game except DD/TD/EJCT = season totals; "
                     "for TO, lower is better.)")
    return "\n".join(lines)


# --- scalar value (interim, pre-z-score) ------------------------------------
# The optimizer needs ONE number per player. Proper roto value is per-league
# z-scores (each category normalized by the league pool's spread) — that needs a
# population fetch and is deferred (see P1 scope note). INTERIM: normalize each
# category by a rough league-wide per-game spread so no single stat (points)
# dominates, sum them, TO negative. Good enough to ORDER players for the
# optimizer; swap in real z-scores when the population fetch lands.
_CAT_SPREAD = {
    "PTS": 6.0, "REB": 3.0, "AST": 2.0, "ST": 0.6, "STL": 0.6, "BLK": 0.6,
    "TO": 1.2, "TOV": 1.2, "3PM": 0.9, "DD": 15.0, "TD": 5.0, "EJCT": 3.0,
}
_NEGATIVE_CATS = {"TO", "TOV"}  # fewer is better


def roto_scalar(stats, categories=None):
    """A single interim value for a player, summed over the league's categories,
    each spread-normalized; TO counts negative. Percentage cats are skipped (a
    ratio's value depends on volume — a z-score refinement, not this sum)."""
    cats = stats.get("cats", {})
    total = 0.0
    for c in (categories or DEFAULT_CATEGORIES):
        if c.endswith("%"):
            continue
        v = cats.get(c)
        if v is None:
            continue
        z = v / _CAT_SPREAD.get(c, 1.0)
        total += -z if c in _NEGATIVE_CATS else z
    return round(total, 2)


# --- real per-league z-scores (draft valuation, #030) -----------------------
# roto_scalar normalizes by GUESSED per-category spreads. A real player-rater
# z-score normalizes each category by the ACTUAL pool's mean + standard
# deviation, so "value" = standard deviations above an average draftable player,
# and no category's raw scale (points ~30 vs steals ~1) dominates. This is the
# population-based valuation #029 P1/P2 deferred ("a z-score needs the whole
# league player pool"); wes_draft supplies the pool. Pure: value is relative to
# exactly the players passed in. Percentages skipped (a rate's value is
# volume-weighted — a further refinement); TO negative.

def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def category_baselines(pool, categories=None):
    """(mean, stdev) per scored counting category across a pool of stat-line
    dicts. Skips % cats, and any cat fewer than 2 players expose (no spread to
    normalize by). stdev floored at a tiny epsilon so a degenerate cat can't
    divide-by-zero. Pure."""
    out = {}
    for c in (categories or DEFAULT_CATEGORIES):
        if c.endswith("%"):
            continue
        vals = [p["cats"][c] for p in pool
                if _num((p.get("cats") or {}).get(c))]
        if len(vals) >= 2:
            out[c] = (statistics.fmean(vals), statistics.pstdev(vals) or 1e-9)
    return out


def rank_by_zscore(pool, categories=None):
    """Rank a player pool by real per-league z-score value (highest first).
    Each returned dict is the input player plus `value` (summed z-score) and
    `zs` (per-category z). The population IS `pool`, so this is only meaningful
    over a realistic draftable set, not two players. Pure/deterministic."""
    cats = categories or DEFAULT_CATEGORIES
    base = category_baselines(pool, cats)
    ranked = []
    for p in pool:
        zs = {}
        for c in cats:
            b = base.get(c)
            v = (p.get("cats") or {}).get(c)
            if b and _num(v):
                mean, sd = b
                zc = (v - mean) / sd
                zs[c] = round(-zc if c in _NEGATIVE_CATS else zc, 2)
        ranked.append({**p, "value": round(sum(zs.values()), 2), "zs": zs})
    ranked.sort(key=lambda x: x["value"], reverse=True)
    return ranked


# --- lineup optimizer (P2), multi-sport (P7 pulled forward) -----------------
# Roster slot label -> the set of positions that satisfy it (None = any player).
# The active-slot STRUCTURE is read off the roster itself (each roster carries
# exactly the league's slots), so no separate settings scrape is needed.
#
# WHY THIS IS A TABLE, NOT A BRANCH: the optimizer below is a pure assignment
# problem (players -> slots, maximize value, respect eligibility) and is
# identical for every sport. Only these tables differ. NFL's FLEX (W/R/T) is
# structurally the same thing as NBA's G (PG|SG) and F (SF|PF), so nothing in
# the solver needed changing to support it.
_SPORTS = {
    "nba": {
        # Yahoo NBA daily-lineup slots.
        "eligibility": {
            "PG": {"PG"}, "SG": {"SG"}, "G": {"PG", "SG"},
            "SF": {"SF"}, "PF": {"PF"}, "F": {"SF", "PF"},
            "C": {"C"}, "UTIL": None, "UTL": None,
        },
        "order": ["PG", "SG", "G", "SF", "PF", "F", "C", "UTIL"],
        # Statuses meaning "do not start" (Yahoo abbreviations). GTD/probable
        # players routinely play, so they are NOT here.
        "out": {"O", "OUT", "INJ", "SUSP", "NA", "IL"},
        "fallback": "UTIL",
        "period": "today",
    },
    "nfl": {
        # Yahoo NFL weekly slots. Yahoo spells flex slots as the eligible
        # positions joined by "/" ("W/R/T"); "FLEX" and "D/ST" show up in some
        # league UIs, and Q/W/R/T + OP are the two superflex spellings.
        "eligibility": {
            "QB": {"QB"}, "RB": {"RB"}, "WR": {"WR"}, "TE": {"TE"},
            "K": {"K"}, "PK": {"K"},
            "DEF": {"DEF"}, "D/ST": {"DEF"}, "DST": {"DEF"},
            "W/R": {"WR", "RB"}, "W/T": {"WR", "TE"}, "R/T": {"RB", "TE"},
            "W/R/T": {"WR", "RB", "TE"}, "FLEX": {"WR", "RB", "TE"},
            "Q/W/R/T": {"QB", "WR", "RB", "TE"}, "OP": {"QB", "WR", "RB", "TE"},
        },
        "order": ["QB", "RB", "WR", "TE", "W/R", "W/T", "R/T", "W/R/T", "FLEX",
                  "Q/W/R/T", "OP", "K", "PK", "DEF", "D/ST", "DST"],
        # NFL: DOUBTFUL effectively means out; QUESTIONABLE does not, so "Q" is
        # deliberately absent. IR/PUP/NFI are season-or-weeks-long designations.
        "out": {"O", "OUT", "IR", "IR-R", "SUSP", "NA", "PUP", "NFI",
                "D", "DOUBTFUL", "DNP"},
        # NFL has no true "any player" slot, so an unrecognized ACTIVE label
        # degrades to the standard flex rather than to a wildcard — a kicker
        # must never become startable at QB just because Yahoo renamed a label.
        "fallback": "W/R/T",
        "period": "this week",
    },
}
# Shared across sports: bench/injury slots are not part of the active lineup.
_BENCH_SLOTS = {"BN", "BE", "IL", "IL+", "IR", "IR+", "IR-R", "NA", "RES"}
DEFAULT_SPORT = "nba"
# Positions that only exist in one sport — enough to identify a roster.
_SPORT_MARKERS = {"nfl": {"QB", "RB", "WR", "TE", "K", "DEF"},
                  "nba": {"PG", "SG", "SF", "PF", "C", "UTIL"}}


def _sport(sport):
    """The slot table for `sport`, defaulting to NBA for an unknown name so a
    bad config degrades to the original behaviour instead of raising."""
    return _SPORTS.get((sport or DEFAULT_SPORT).lower(), _SPORTS[DEFAULT_SPORT])


def infer_sport(slots):
    """Guess the sport from a roster's slot labels, so callers that already know
    the sport can stay silent and a roster can't be scored against the wrong
    table. Counts unambiguous position markers ('QB' is NFL-only, 'PG' is
    NBA-only) and takes the winner; ties / no evidence -> DEFAULT_SPORT."""
    seen = {s.strip().upper() for s in (slots or []) if s}
    hits = {sp: len(seen & marks) for sp, marks in _SPORT_MARKERS.items()}
    best_sport = max(hits, key=lambda sp: hits[sp])
    if hits[best_sport] == 0 or list(hits.values()).count(hits[best_sport]) > 1:
        return DEFAULT_SPORT
    return best_sport


def _startable(p, sport=DEFAULT_SPORT):
    """Can this player be started this period? They must have a game (NFL: not on
    a bye) AND not be ruled out. Absent `playing` (offseason / unknown schedule)
    counts as not playing."""
    return bool(p.get("playing")) and \
        (p.get("status") or "").upper() not in _sport(sport)["out"]


def _slot_type(slot, sport=DEFAULT_SPORT):
    """Normalize a roster slot label to a known active-slot type, or None if it's
    a bench/IL slot. Unknown *active* labels fall back to the sport's widest
    non-wildcard slot (NBA: UTIL / any; NFL: W/R/T flex)."""
    s = (slot or "").strip().upper()
    if s in _BENCH_SLOTS:
        return None
    tbl = _sport(sport)
    return s if s in tbl["eligibility"] else tbl["fallback"]


def optimize_lineup(players, slots, sport=None):
    """Exact optimal lineup for the period: assign startable players to the
    active slots to MAXIMIZE total value, respecting position eligibility.
    Pure/deterministic (§8.2) — no LLM, no network. Sport-agnostic: NBA daily
    and NFL weekly are the same assignment problem (see _SPORTS).

      players: [{name, positions:[...], value:float, playing:bool, status}]
      slots:   the roster's slot labels (NBA ['PG',...,'UTIL','BN','IL'];
               NFL ['QB','RB','RB','WR','WR','TE','W/R/T','K','DEF','BN','IR'])
      sport:   'nba' | 'nfl'; None infers it from `slots` (see infer_sport)

    Returns {starters:[{slot,name,value}], bench:[name], empty_slots:[type],
             total:float}. Players without a game / ruled out are benched; a
    startable player left out (lineup full or no eligible open slot) is benched
    too. Same-type slots are interchangeable, so a starter's `slot` is its type.

    Method: capacity DP over slot types — state = (player index, remaining
    capacity per type). Optimal by construction; state space is tiny for either
    sport's roster (≤ ~16 players, ≤ ~10 distinct slot types)."""
    sport = sport or infer_sport(slots)
    tbl = _sport(sport)
    elig_tbl = tbl["eligibility"]
    order = [t for t in tbl["order"]
             if any(_slot_type(s, sport) == t for s in slots)]
    cap0 = tuple(sum(1 for s in slots if _slot_type(s, sport) == t) for t in order)

    startable, benched = [], []
    for p in players:
        (startable if _startable(p, sport) else benched).append(p)

    def _elig(p):
        pos = {x.strip().upper() for x in (p.get("positions") or []) if x}
        return tuple((elig_tbl[t] is None or bool(pos & elig_tbl[t]))
                     for t in order)
    elig = [_elig(p) for p in startable]
    # `value: None` means UNKNOWN (no stat line found), which is NOT the same as
    # "worth 0" — but the DP needs a number, so unknowns score 0 and are recorded
    # instead. Conflating the two produced a real wrong lineup on 2026-07-29: a
    # pool missing every WR valued Ja'Marr Chase at 0.0, so he was benched behind
    # a replacement-level rookie and the output looked perfectly confident.
    # Design §8.8: don't act on incomplete data, say so.
    val = [float(p.get("value") or 0.0) for p in startable]
    unknown = [p["name"] for p in startable if p.get("value") is None]

    import functools

    @functools.lru_cache(maxsize=None)
    def best(i, caps):
        """(max value, starters used, assignment tuple) for players i.. given
        remaining caps. assignment[k] = slot-type index for player i+k, or None
        if benched.

        Maximizes (value, starters_filled) LEXICOGRAPHICALLY. The second term is
        a tie-break only — it can never cost value — and it exists because a
        strict value comparison benches a player worth exactly 0.0 and reports
        their slot empty, which is both useless and looks like a bug. Zero-value
        players are ordinary in practice (pre-season, or any player the valuer
        has no stats for), and a real DEF was benched this way on the first live
        run. Filling a slot is never worse than leaving it open."""
        if i == len(startable):
            return 0.0, 0, ()
        bv, bc, ba = best(i + 1, caps)      # option A: bench player i
        best_v, best_c, best_a = bv, bc, (None,) + ba
        for ti in range(len(order)):        # option B: start in an eligible open slot
            if caps[ti] > 0 and elig[i][ti]:
                nxt = caps[:ti] + (caps[ti] - 1,) + caps[ti + 1:]
                v, c, a = best(i + 1, nxt)
                if (val[i] + v, c + 1) > (best_v, best_c):
                    best_v, best_c, best_a = val[i] + v, c + 1, (ti,) + a
        return best_v, best_c, best_a

    total, _, assign = best(0, cap0)
    best.cache_clear()  # closure cache — drop it so nothing lingers between calls

    starters, used = [], list(cap0)
    for idx, (p, ti) in enumerate(zip(startable, assign)):
        if ti is None:
            benched.append(p)
        else:
            starters.append({"slot": order[ti], "name": p["name"], "value": val[idx]})
            used[ti] -= 1
    empty = [order[ti] for ti in range(len(order)) for _ in range(used[ti])]
    return {
        "starters": starters,
        "bench": [p["name"] for p in benched],
        "empty_slots": empty,
        "total": round(total, 2),
        "sport": sport,
        # Startable players whose value could not be determined. Non-empty means
        # this lineup is a guess in part — surface it, never swallow it.
        "unknown_value": unknown,
    }


def format_lineup(result, team_name=""):
    """Compact, spoken/typed-friendly optimal lineup for the model to relay."""
    period = _sport(result.get("sport"))["period"]   # NBA "today" / NFL "this week"
    if not result["starters"] and not result["empty_slots"]:
        return (f"No startable players {period} — nobody on the roster "
                f"has a game.")
    head = f"Optimal lineup{f' — {team_name}' if team_name else ''}:"
    lines = [head]
    for s in result["starters"]:
        lines.append(f"  {s['slot']}: {s['name']}  ({s['value']:g})")
    if result["empty_slots"]:
        lines.append("  Empty (no eligible startable player): "
                     + ", ".join(result["empty_slots"]))
    if result["bench"]:
        lines.append("  Bench: " + ", ".join(result["bench"]))
    lines.append(f"Projected value total: {result['total']:g}.")
    if result.get("unknown_value"):
        # Stated plainly, because a lineup built on missing data still LOOKS
        # authoritative and the model would otherwise relay it as certain.
        lines.append(
            "  WARNING — no stats found for "
            + ", ".join(result["unknown_value"])
            + f"; they were treated as 0, so this lineup may be wrong. "
              f"({len(result['unknown_value'])} of "
              f"{len(result['starters']) + len(result['bench'])} players.)")
    return "\n".join(lines)


def _league_categories():
    """The configured default team's scoring categories (cached in-process, since
    they ~never change), or the standard roto set if nothing's configured or the
    live scrape fails."""
    team, _ = wes_yahoo._resolve_team()
    if not team:
        return DEFAULT_CATEGORIES
    league = team.get("league_key", "")
    if league not in _cat_cache:
        _cat_cache[league] = wes_yahoo.league_categories(league) or DEFAULT_CATEGORIES
    return _cat_cache[league]


def fantasy_player_value(player, versus=None):
    """Tool entry: value a player (optionally vs another) in the owner's league's
    scoring. Resolves the league's categories from the configured team."""
    return player_value(player, categories=_league_categories(), versus=versus)


# --- NFL: the league's REAL points scoring (#029 P7) -------------------------
# The composition seam. wes_yahoo scrapes text and knows no football; wes_nfl
# knows football and touches no browser; this module already imports both, so
# the wiring belongs here and both sides stay unit-testable in isolation.
_nfl_scoring_cache = {}   # league_key -> parsed scoring dict


def nfl_league_scoring(league_key, _lines_fn=None):
    """This NFL league's real scoring: {"weights", "tiers", "parsed", "unknown"}.

    Falls back to wes_nfl's defaults (half-PPR + Yahoo's standard ladder) when the
    scrape fails, so valuation degrades to a sane guess instead of to zeros.
    Cached per league — scoring settings essentially never change mid-season."""
    if league_key in _nfl_scoring_cache:
        return _nfl_scoring_cache[league_key]
    fetch = _lines_fn or wes_yahoo.league_settings_lines
    lines = fetch(league_key)
    parsed = wes_nfl.parse_scoring(lines if isinstance(lines, list) else [])
    if not isinstance(lines, list):
        # Degradation string from the scrape layer — note it, don't raise.
        print(f"[fantasy] nfl scoring scrape failed for {league_key}: "
              f"{lines!r:.120}", flush=True)
    _nfl_scoring_cache[league_key] = parsed
    return parsed


def nfl_league_slots(league_key, _lines_fn=None):
    """The league's authoritative roster slots from settings, e.g.
    ['QB','WR','WR','RB','RB','TE','W/R/T','K','DEF','BN',...,'IR'].

    Preferred over deriving slots from a scraped roster: it works PRE-DRAFT (no
    roster yet) and includes slots no player currently occupies."""
    fetch = _lines_fn or wes_yahoo.league_settings_lines
    lines = fetch(league_key)
    return wes_nfl.parse_roster_slots(lines if isinstance(lines, list) else [])


def _playing_today(player):
    """Does this rostered player's NBA team have a game today? Offseason (no
    games, blank team) -> False."""
    return wes_nba.team_playing_today(player.get("team", ""))


def _player_scalar(player, categories):
    """Fetch a rostered player's season stats and reduce to the interim scalar.
    None when stats can't be fetched — UNKNOWN, not zero, so `optimize_lineup`
    reports it instead of silently ranking the player as worthless (see the
    `unknown_value` note there)."""
    stats = wes_nba.player_season_stats(player.get("name", ""))
    return roto_scalar(stats, categories) if isinstance(stats, dict) else None


# --- NFL: weekly availability + values from the ESPN pool --------------------
def _nfl_playing(player):
    """Is this player available THIS WEEK? NFL is weekly, not daily: the question
    is "not on a bye" rather than "has a game today".

    Reads the `game` cell wes_yahoo captures ("Sun 1:25 pm vs Was"). Empty means
    no game is shown, which we treat as not playing — failing safe, since a
    player we can't confirm shouldn't take a starting slot from one we can.

    CAVEAT: Yahoo renders a bye as "Bye" in that cell, but a real bye could NOT
    be observed on 2026-07-29 (pre-season; every row showed a Week 1 game). The
    substring check is from Yahoo's documented rendering, not from a captured
    example — re-verify in-season, ideally week 5+ when byes start."""
    game = " ".join((player.get("game") or "").split()).lower()
    if not game:
        return False
    return "bye" not in game


def _norm_name(name):
    """Loose key for matching a Yahoo roster name to an ESPN pool name (case,
    punctuation and suffix noise differ: "Ja'Marr Chase", "James Cook III")."""
    return "".join(ch for ch in str(name or "").lower() if ch.isalnum())


def _nfl_value_map(league_key, _pool_fn=None, _scoring_fn=None):
    """{normalized name -> fantasy points} for the NFL pool under THIS league's
    scoring, plus the sorts that failed. Ranked PER GAME so an injury-shortened
    season doesn't read as a bad player."""
    scoring = (_scoring_fn or nfl_league_scoring)(league_key)
    pool, failed = (_pool_fn or wes_nfl.pool_by_position)()
    ranked = wes_nfl.rank_by_points([wes_nfl.per_game(p) for p in pool],
                                   scoring["weights"], scoring["tiers"])
    return {_norm_name(p["name"]): p["value"] for p in ranked}, failed


def fantasy_optimize_lineup(team=None, _players_fn=None, _playing_fn=None,
                            _value_fn=None, _slots_fn=None, _valmap_fn=None):
    """P2 tool entry: recommend the optimal starting lineup for a configured team.
    ADVISE / DRY-RUN only — it NEVER writes to Yahoo (that's P3's gated executor).
    Degrades to a string on any problem, never raises into a turn.

    Multi-sport. The two sports differ in three places and nowhere else:
      period      NBA = today (per-game locks)   NFL = this week (one Sunday lock)
      availability "team has a game today"       "not on a bye"
      valuation    roto scalar over categories   fantasy points under league weights
    NBA additionally hides eligible positions in the offseason, which is why the
    blank-positions guard below exists.

    Injectables let the whole pipeline be unit-tested from fixtures without the
    network (design §9)."""
    chosen, err = wes_yahoo._resolve_team(team)
    if err:
        return err
    if chosen is None:
        return "No fantasy team is configured yet — set up teams.yaml first."
    team_key, name = chosen.get("team_key", ""), chosen.get("name", "")
    league_key = chosen.get("league_key", "") or team_key
    sport = str(chosen.get("sport") or wes_yahoo._sport_of(team_key)).lower()
    players = (_players_fn or wes_yahoo.roster_players)(team_key)
    if not isinstance(players, list):
        return players  # degradation string from the scraper
    if not players:
        return "That roster came back empty."
    # No eligible positions => can't respect slot eligibility. This is the NBA
    # offseason state (Yahoo blanks them); fail safe rather than guess (§8.8).
    if not any(p.get("positions") for p in players):
        return ("I can't build a lineup yet — Yahoo isn't showing player "
                "positions (they stay blank until the season starts).")

    warn = ""
    if sport == "nfl":
        playing = _playing_fn or _nfl_playing
        if _value_fn:
            value = _value_fn
        else:
            vmap, failed = (_valmap_fn or _nfl_value_map)(league_key)
            # .get returns None for a player with no stat line — UNKNOWN, which
            # optimize_lineup reports rather than scoring as a real zero.
            value = lambda p: vmap.get(_norm_name(p.get("name")))  # noqa: E731
            if failed:
                warn = ("\n  NOTE — part of the player pool didn't load "
                        f"({len(failed)} of {len(wes_nfl._POOL_SORTS)} position "
                        "groups), so some values may be missing.")
        slots = (_slots_fn or nfl_league_slots)(league_key) \
            or [p.get("slot", "") for p in players]
        no_games = ("Everyone on your roster is on a bye or has no game this "
                    "week — there's no lineup to set.")
    else:
        cats = _league_categories()
        playing = _playing_fn or _playing_today
        value = _value_fn or (lambda p: _player_scalar(p, cats))
        slots = [p.get("slot", "") for p in players]
        no_games = "No games on your roster today — there's no lineup to set."

    enriched = [{**p, "playing": playing(p), "value": value(p)} for p in players]
    if not any(e["playing"] for e in enriched):
        return no_games
    result = optimize_lineup(enriched, slots, sport)
    return format_lineup(result, team_name=name) + warn
