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


# --- daily lineup optimizer (P2) --------------------------------------------
# Yahoo NBA daily-lineup slots -> the positions that satisfy them (None = any).
# The active-slot STRUCTURE is read off the roster itself (each roster carries
# exactly the league's slots), so no separate settings scrape is needed.
_SLOT_ELIGIBILITY = {
    "PG": {"PG"}, "SG": {"SG"}, "G": {"PG", "SG"},
    "SF": {"SF"}, "PF": {"PF"}, "F": {"SF", "PF"},
    "C": {"C"}, "UTIL": None, "UTL": None,
}
_BENCH_SLOTS = {"BN", "BE", "IL", "IL+", "IR", "NA"}  # not part of the active lineup
_SLOT_ORDER = ["PG", "SG", "G", "SF", "PF", "F", "C", "UTIL"]
# Injury/availability statuses that mean "do not start" (Yahoo abbreviations).
_OUT_STATUS = {"O", "OUT", "INJ", "SUSP", "NA", "IL"}


def _startable(p):
    """Can this player be started today? They must have a game AND not be ruled
    out. Absent `playing` (offseason / unknown schedule) counts as not playing."""
    return bool(p.get("playing")) and \
        (p.get("status") or "").upper() not in _OUT_STATUS


def _slot_type(slot):
    """Normalize a roster slot label to a known active-slot type, or None if it's
    a bench/IL slot. Unknown *active* labels fall back to UTIL (any-eligible)."""
    s = (slot or "").upper()
    if s in _BENCH_SLOTS:
        return None
    return s if s in _SLOT_ELIGIBILITY else "UTIL"


def optimize_lineup(players, slots):
    """Exact optimal daily lineup: assign startable players to the active slots
    to MAXIMIZE total value, respecting position eligibility. Pure/deterministic
    (§8.2) — no LLM, no network.

      players: [{name, positions:[...], value:float, playing:bool, status}]
      slots:   the roster's slot labels (e.g. ['PG','SG',...,'UTIL','BN','IL'])

    Returns {starters:[{slot,name,value}], bench:[name], empty_slots:[type],
             total:float}. Players without a game / ruled out are benched; a
    startable player left out (lineup full or no eligible open slot) is benched
    too. Same-type slots are interchangeable, so a starter's `slot` is its type.

    Method: capacity DP over slot types — state = (player index, remaining
    capacity per type). Optimal by construction; state space is tiny for an NBA
    roster (≤ ~15 players, ≤ 8 slot types)."""
    order = [t for t in _SLOT_ORDER
             if any(_slot_type(s) == t for s in slots)]
    cap0 = tuple(sum(1 for s in slots if _slot_type(s) == t) for t in order)

    startable, benched = [], []
    for p in players:
        (startable if _startable(p) else benched).append(p)

    def _elig(p):
        pos = set(p.get("positions") or [])
        return tuple((_SLOT_ELIGIBILITY[t] is None or bool(pos & _SLOT_ELIGIBILITY[t]))
                     for t in order)
    elig = [_elig(p) for p in startable]
    val = [float(p.get("value") or 0.0) for p in startable]

    import functools

    @functools.lru_cache(maxsize=None)
    def best(i, caps):
        """(max value, assignment tuple) for players i.. given remaining caps.
        assignment[k] = slot-type index for player i+k, or None if benched."""
        if i == len(startable):
            return 0.0, ()
        bv, ba = best(i + 1, caps)          # option A: bench player i
        best_v, best_a = bv, (None,) + ba
        for ti in range(len(order)):        # option B: start in an eligible open slot
            if caps[ti] > 0 and elig[i][ti]:
                nxt = caps[:ti] + (caps[ti] - 1,) + caps[ti + 1:]
                v, a = best(i + 1, nxt)
                if val[i] + v > best_v:
                    best_v, best_a = val[i] + v, (ti,) + a
        return best_v, best_a

    total, assign = best(0, cap0)
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
    }


def format_lineup(result, team_name=""):
    """Compact, spoken/typed-friendly optimal lineup for the model to relay."""
    if not result["starters"] and not result["empty_slots"]:
        return "No startable players today — nobody on the roster has a game."
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


def _playing_today(player):
    """Does this rostered player's NBA team have a game today? Offseason (no
    games, blank team) -> False."""
    return wes_nba.team_playing_today(player.get("team", ""))


def _player_scalar(player, categories):
    """Fetch a rostered player's season stats and reduce to the interim scalar.
    0.0 if stats can't be fetched (so a lookup miss just benches them, no crash)."""
    stats = wes_nba.player_season_stats(player.get("name", ""))
    return roto_scalar(stats, categories) if isinstance(stats, dict) else 0.0


def fantasy_optimize_lineup(team=None, _players_fn=None, _playing_fn=None,
                            _value_fn=None):
    """P2 tool entry: recommend today's optimal starting lineup for a configured
    team. ADVISE / DRY-RUN only — it NEVER writes to Yahoo (that's P3's gated
    executor). Degrades to a string on any problem, including the offseason:
    Yahoo hides eligible positions and there are no games until October, so
    there's nothing to optimize yet (re-verify in-season via WES_YAHOO_LIVE=1).

    Injectables (_players_fn/_playing_fn/_value_fn) let the whole pipeline be
    unit-tested from fixtures without the network (design §9)."""
    chosen, err = wes_yahoo._resolve_team(team)
    if err:
        return err
    if chosen is None:
        return "No fantasy team is configured yet — set up teams.yaml first."
    team_key, name = chosen.get("team_key", ""), chosen.get("name", "")
    players = (_players_fn or wes_yahoo.roster_players)(team_key)
    if not isinstance(players, list):
        return players  # degradation string from the scraper
    if not players:
        return "That roster came back empty."
    # No eligible positions => can't respect slot eligibility. This is the
    # offseason state (Yahoo blanks them); fail safe rather than guess (§8.8).
    if not any(p.get("positions") for p in players):
        return ("I can't build a lineup yet — Yahoo isn't showing player "
                "positions (they stay blank until the season starts).")
    cats = _league_categories()
    playing = _playing_fn or _playing_today
    value = _value_fn or (lambda p: _player_scalar(p, cats))
    enriched = [{**p, "playing": playing(p), "value": value(p)} for p in players]
    if not any(e["playing"] for e in enriched):
        return "No games on your roster today — there's no lineup to set."
    result = optimize_lineup(enriched, [p.get("slot", "") for p in players])
    return format_lineup(result, team_name=name)
