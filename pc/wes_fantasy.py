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
