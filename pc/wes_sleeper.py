"""Sleeper platform adapter — READ ONLY (ticket #039).

Sleeper is a second fantasy platform alongside Yahoo, and it is a much kinder
one to integrate: a documented, unauthenticated JSON API instead of Playwright
against a logged-in DOM. Concretely, compared with `wes_yahoo`:

  - League scoring arrives as STRUCTURED JSON (43 numeric settings), so there is
    no text parsing and no section-aware scraper to drift. `parse_scoring` is a
    lookup table, not a parser.
  - Rosters, free agents and slots are JSON. No browser, no session, no cookies.
  - `/v1/players/nfl` carries `espn_id`, `gsis_id` and `yahoo_id`, so a Sleeper
    player joins to our ESPN valuations (and to nflverse) BY ID rather than by
    fuzzy name match — the identity problem that would otherwise dominate this
    work is simply absent.

WHAT THIS MODULE DOES NOT DO YET: **write.** Sleeper's public v1 API is
read-only, so writes go through the browser (owner decision 2026-08-14, same
approach as Yahoo). The SESSION for that is here; the lineup and add/drop
gestures are NOT, because they cannot be reconned yet: the league is
`pre_draft` and every roster holds 0 players, so the controls that would be
automated do not exist on the page. Building them from guesswork is exactly how
the Yahoo swapper benched the wrong player, so they wait for a real roster.

Layering (docs/data-architecture.md): raw + fantasy-data. Every fetch goes
through `wes_http`; every parser below it is PURE and takes already-fetched
payloads, so the translation is testable with no network at all.
"""
import os
import re
import time

import wes_http
import wes_nfl

BASE = "https://api.sleeper.app/v1"
WEB = "https://sleeper.com"

# The browser profile that holds the Sleeper login. Separate from the Yahoo one
# so a re-login on either platform cannot disturb the other. PC-local, never the
# repo: it contains real session cookies.
PROFILE_DIR = os.environ.get(
    "WES_SLEEPER_PROFILE_DIR",
    os.path.join(os.path.expanduser("~"), "wes-pc", "sleeper_profile"))
# HEADLESS BY DEFAULT, unlike the Yahoo session. The original reasoning —
# "the one-time login is interactive, and a headless window the owner cannot see
# is a bad place to discover that a session expired" — was written before token
# injection. Login is not interactive here, `authenticate` returns a boolean,
# and the draft-day pre-flight checks the session explicitly, so a visible
# window buys nothing and costs the owner a Chrome popup stealing focus every
# pick, fifteen times an afternoon, on the machine they are working on.
#
# Verified identical, not assumed: a full headless mock drafted 15 of 15 with
# zero substitutions in 620s, against 622s headed (2026-08-16). Same machine,
# same browser, same profile — only the window is gone.
#
# Set WES_SLEEPER_HEADLESS=0 to watch it work, which is worth doing when the
# draft room's DOM changes under us.
HEADLESS = os.environ.get("WES_SLEEPER_HEADLESS", "1") == "1"
BROWSER_CHANNEL = os.environ.get("WES_SLEEPER_BROWSER_CHANNEL", "chrome")

# The account token, from the PC user environment (never the repo), same
# convention as WES_DISCORD_TOKEN.
#
# WHY A TOKEN AND NOT AN INTERACTIVE LOGIN: sleeper.com's login form is behind
# hCaptcha, which is built to detect exactly the browser Playwright launches —
# the owner could not get through it by hand in the automation profile, and
# even one success would not last, since hCaptcha re-challenges. But the captcha
# guards OBTAINING a session, not PRESENTING one. Injecting an existing token
# walks straight past it, and the app then bootstraps its own session state as
# if a human had signed in (verified 2026-08-14).
def _read_token(username=None):
    """The token for `username`, with a fallback to the PERSISTED user-scope
    value on Windows.

    Looks for WES_SLEEPER_TOKEN_<USERNAME> first, then the shared
    WES_SLEEPER_TOKEN. So a second account is additive rather than a
    replacement, and the account that holds the real league team keeps working
    whatever else is configured.

    A shell opened before the variable was set does not inherit it, and the
    failure mode is quiet and expensive: a mock draft ran for seven minutes,
    stood down on every turn with "no WES_SLEEPER_TOKEN", let cpu_autopick take
    all fifteen picks, and printed a perfectly plausible roster that proved
    nothing (2026-08-15). The value is already on the machine; there is no
    reason for a stale shell to be the thing that decides whether we can draft.
    """
    names = []
    if username:
        names.append("WES_SLEEPER_TOKEN_"
                     + "".join(c for c in username.upper() if c.isalnum()))
    names.append("WES_SLEEPER_TOKEN")
    for var in names:
        tok = os.environ.get(var, "")
        if tok:
            return tok
    if os.name != "nt":
        return ""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            for var in names:
                try:
                    got = str(winreg.QueryValueEx(key, var)[0] or "")
                except OSError:
                    continue
                if got:
                    return got
    except OSError:
        return ""
    return ""


# WHO WES IS ON SLEEPER. A setting, not a constant: the owner has more than one
# account (a personal one that holds the real league team, and a bot account for
# mocks), and the account has to match the TOKEN or every write lands as the
# wrong person -- or nowhere, since the seat lookups key off the display name.
USERNAME = os.environ.get("WES_SLEEPER_USER", "awarmwalrus")

# PAIRED WITH THE ACCOUNT, so the two cannot drift apart. A per-account
# variable wins over the shared one, which means adding a second account never
# displaces the first -- and the first is the one holding the real league team.
# Mismatching a token and a username is the failure this prevents, and it is
# quiet: the writes would land as somebody else.
TOKEN = _read_token(USERNAME)

# The single localStorage key Sleeper's web app reads the token from. Pinned by
# testing candidates ONE AT A TIME against a cleared store: of `token`,
# `user_token`, `auth_token`, `jwt` and `access_token`, only this one gets past
# the login wall. Injecting all five worked too, but shipping the shotgun would
# have kept "working" if the real key changed — right up until it didn't.
TOKEN_KEY = "token"

# League metadata changes rarely; the player dump changes ~daily and Sleeper's
# docs ask callers not to pull it more than once a day (it is 14MB).
LEAGUE_TTL = float(900)
PLAYERS_TTL = float(6 * 3600)

# How far back to look for a positional run. Roughly one trip round a 12-team
# board, which is the horizon that matters: what happens inside it decides
# whether a tier survives until your next turn.
RUN_WINDOW = 12


def _get(path, ttl=LEAGUE_TTL, _get_fn=None):
    return (_get_fn or wes_http.get_json)(BASE + path, ttl=ttl)


# --- scoring ----------------------------------------------------------------
# Sleeper's keys -> the canonical stat keys `wes_nfl.fantasy_points` scores.
# Written out rather than derived: the two vocabularies genuinely differ, and a
# silent mismatch here would misprice every player in the league.
_SCORING_MAP = {
    "pass_yd": "PassYds", "pass_td": "PassTD", "pass_int": "Int",
    "rush_yd": "RushYds", "rush_td": "RushTD",
    "rec": "Rec", "rec_yd": "RecYds", "rec_td": "RecTD",
    "fum_lost": "FumLost",
    "xpm": "XP", "xpmiss": "XPMiss", "fgmiss": "FGMiss",
    "fgm_0_19": "FG0_19", "fgm_20_29": "FG20_29", "fgm_30_39": "FG30_39",
    "fgm_40_49": "FG40_49", "fgm_50_59": "FG50", "fgm_50p": "FG50",
    # A 60+ yarder is still a made 50+ FG in every stat feed we have, so it
    # folds into the same bucket. Leaving it unmapped (as it was on first run
    # against the real league) would undervalue a big-leg kicker by the whole
    # difference between the 50-59 and 60+ rates.
    "fgm_60p": "FG50",
    # Team defence / special teams.
    "sack": "Sack", "int": "DefInt", "fum_rec": "FumRec",
    "def_td": "DefTD", "safe": "Safety", "blk_kick": "BlkKick",
    "st_td": "DefRetTD", "def_st_td": "DefRetTD", "pr_td": "DefRetTD",
    "kr_td": "DefRetTD",
}
# All three two-point flavours collapse to one canonical key. They are
# separately configurable in Sleeper but always equal in practice; if a league
# ever splits them, the LARGEST wins, so a projection can't be understated.
_TWO_PT_KEYS = ("pass_2pt", "rush_2pt", "rec_2pt")

# Sleeper expresses points-allowed as one flat setting per band. Ours is an
# ordered (max_allowed, points) ladder, so the bands have to be re-expressed —
# the upper bound of each band, in ascending order.
_PTS_ALLOW_BANDS = [
    ("pts_allow_0", 0), ("pts_allow_1_6", 6), ("pts_allow_7_13", 13),
    ("pts_allow_14_20", 20), ("pts_allow_21_27", 27), ("pts_allow_28_34", 34),
    ("pts_allow_35p", 10 ** 6),
]

# Settings we knowingly ignore, so they don't show up as "unknown" noise: IDP,
# kick/punt yardage, bonuses and per-game rate stats we do not model.
_IGNORED_PREFIXES = ("idp_", "bonus_", "pass_cmp", "pass_att", "rush_att",
                     "rec_tgt", "pass_inc", "pass_sack", "fga", "fgm_yds",
                     "punt", "kick", "pass_fd", "rush_fd", "rec_fd", "fum",
                     "def_", "st_", "tkl", "sack_yd", "qb_hit", "int_ret",
                     "anytime")


def parse_scoring(scoring_settings):
    """Sleeper `scoring_settings` -> our {weights, tiers, unknown} contract.

    PURE. Mirrors `wes_fantasy.nfl_league_scoring`'s output shape exactly so the
    valuer, optimizer and executor cannot tell which platform a league came
    from — that indifference is the whole point of an adapter.

    `unknown` lists settings we saw but do not model, kept for the same reason
    the Yahoo parser keeps it: a scoring rule we silently drop is a systematic
    mispricing, and it should be visible rather than inferred from bad advice."""
    src = scoring_settings if isinstance(scoring_settings, dict) else {}
    weights = dict(wes_nfl.DEFAULT_SCORING)
    seen, unknown = set(), []

    for key, val in src.items():
        try:
            num = float(val)
        except (TypeError, ValueError):
            continue
        if key in _SCORING_MAP:
            weights[_SCORING_MAP[key]] = num
            seen.add(key)
        elif key in _TWO_PT_KEYS:
            weights["2PT"] = max(num, weights.get("2PT", 0.0)) \
                if any(k in seen for k in _TWO_PT_KEYS) else num
            seen.add(key)
        elif any(key == b[0] for b in _PTS_ALLOW_BANDS):
            seen.add(key)          # handled as tiers below
        elif key.startswith(_IGNORED_PREFIXES):
            seen.add(key)
        else:
            unknown.append(key)

    # Points-allowed ladder. Only build one if the league actually configures
    # it; otherwise keep our default rather than inventing a flat zero ladder
    # that would value every defence identically.
    bands = [(cap, float(src[k])) for k, cap in _PTS_ALLOW_BANDS if k in src]
    tiers = bands if bands else list(wes_nfl.POINTS_ALLOWED_TIERS)

    return {"weights": weights, "tiers": tiers, "unknown": sorted(unknown)}


# --- roster slots -----------------------------------------------------------
# Sleeper's slot vocabulary -> the one the optimizer speaks (Yahoo-shaped,
# because that is what everything upstream already uses).
_SLOT_MAP = {
    "QB": "QB", "RB": "RB", "WR": "WR", "TE": "TE", "K": "K",
    "DEF": "DEF", "DST": "DEF",
    "FLEX": "W/R/T", "REC_FLEX": "W/R", "SUPER_FLEX": "Q/W/R/T",
    "WRRB_FLEX": "W/R", "IDP_FLEX": "IDP",
    "BN": "BN", "IR": "IR", "TAXI": "TAXI",
}


def parse_roster_slots(roster_positions):
    """Sleeper `roster_positions` -> our slot names, order preserved. PURE.

    An unrecognised slot passes through unchanged rather than being dropped: a
    slot we cannot name is still a slot that must be filled, and silently
    losing one would let the optimizer field an illegal lineup."""
    return [_SLOT_MAP.get(str(p).upper(), str(p).upper())
            for p in (roster_positions or [])]


# --- player identity --------------------------------------------------------
_players_cache = {"at": 0.0, "index": None}


def parse_players(payload):
    """The 14MB `/players/nfl` dump -> a slim id index. PURE.

    Keeps only what a join needs. The dump is ~12k players including practice
    squads and retirees; holding all of it to answer "who is player 4034" would
    cost 14MB of resident memory for a few hundred bytes of signal."""
    out = {}
    for pid, p in (payload or {}).items():
        if not isinstance(p, dict):
            continue
        pos = p.get("position")
        name = p.get("full_name") or p.get("last_name") or ""
        # Team defences have no `full_name`; they carry the club split across
        # first/last ("Houston" / "Texans"). Use BOTH. Taking last_name alone
        # gave "Texans", which is not what the draft room renders — the row
        # reads "Houston Texans", the exact-name match missed, and eight
        # attempts to draft a defence were reported as "he is gone". We
        # finished that mock with four tight ends and no defence at all
        # (2026-08-15).
        if pos == "DEF":
            name = (f"{p.get('first_name') or ''} "
                    f"{p.get('last_name') or ''}").strip()
            if not name:
                name = p.get("team") or str(pid)
        if not name:
            continue
        out[str(pid)] = {
            "name": name,
            "positions": [pos] if pos else [],
            "team": p.get("team"),
            "espn_id": str(p["espn_id"]) if p.get("espn_id") else None,
            "gsis_id": p.get("gsis_id"),
            "yahoo_id": str(p["yahoo_id"]) if p.get("yahoo_id") else None,
            # Carried for the CROSSWALK, not for fantasy: birth date is the
            # strongest disambiguator when two players share a name and
            # position, and dropping it left 31 such pairs unresolvable (#039).
            "birth_date": p.get("birth_date") or "",
            # `status` has meant INJURY status since the roster reader was
            # written (it is what a roster row displays), and parse_roster
            # depends on that. The two explicit names below are the ones new
            # code should use, because Sleeper's own `status` field means
            # something else entirely and reading one for the other is a bug
            # waiting to happen.
            "status": p.get("injury_status") or "",
            "injury_status": p.get("injury_status") or "",
            "roster_status": p.get("status") or "",
            # Sleeper's own ranking — the order the draft room renders in, and
            # the closest thing to a market price we get for free.
            "search_rank": _market_rank(p.get("search_rank")),
            # Where he sits on his own team's depth chart (1 = starter). 75%
            # coverage on rostered skill players. This is what makes a HANDCUFF
            # nameable rather than guessed at: the RB2 behind a back we already
            # own inherits the touches if that back goes down.
            "depth_chart_order": (p.get("depth_chart_order")
                                  if isinstance(p.get("depth_chart_order"), int)
                                  else None),
            # QUALITATIVE NOTES WE ALREADY PAY FOR. All of this arrives in the
            # 14MB dump we fetch daily and was being reduced to the single word
            # "PUP". "Knee - ACL, Surgery" is a different fact from "PUP", and
            # the difference is the whole question of whether to draft him.
            # Coverage measured 2026-08-21: body part 570/648 injured players,
            # notes 84/648, news timestamp 580/648.
            "injury_body_part": p.get("injury_body_part") or "",
            "injury_notes": p.get("injury_notes") or "",
            # When Sleeper last had news about him, in ms. Stale news on an
            # injured player is itself information: nothing new in three weeks
            # reads differently from a report this morning.
            "news_updated": (p.get("news_updated")
                             if isinstance(p.get("news_updated"), int)
                             else None),
            "age": p.get("age") if isinstance(p.get("age"), int) else None,
            "years_exp": (p.get("years_exp")
                          if isinstance(p.get("years_exp"), int) else None),
            "college": p.get("college") or "",
        }
    return out


# Ranks run 1..997 (719 distinct). 999 and 9999999 are SENTINELS for "not
# ranked", carried by 359 and 9468 players. Treating them as ranks would file a
# fifth of the pool at "rank 999" and make it look merely unpopular rather than
# unranked — the same distinction as an unknown bye, and the same rule: unknown
# is None, never a number that happens to sort last.
_RANK_SENTINEL = 999


# Cannot take the field, or is not on an NFL roster at all. Kept deliberately
# SHORT, because a hard exclusion is the model never seeing the player: these
# are not judgment calls, and everything that IS one belongs in front of it.
UNDRAFTABLE_INJURY = {"IR", "Out", "Suspended", "NA", "DNR"}
UNDRAFTABLE_ROSTER = {"Inactive", "Practice Squad", "Non Football Injury"}

# PUP is NOT in the list above, and that is a deliberate reversal. My first
# version excluded it and silently deleted George Kittle (TE, 10.38 — a top-12
# tight end) along with Alec Pierce and Zach Charbonnet. Preseason PUP with an
# ACTIVE roster status is frequently just a camp designation, and the feed
# carries no return date to tell the two apart. Excluding on ambiguity is the
# same error as reporting an unrendered list as an absent player: we do not
# know, so say so rather than decide. It costs value instead, and the model is
# told why.
RISKY_INJURY = {"PUP", "Doubtful", "Questionable"}
INJURY_PENALTY = {"PUP": 4.0, "Doubtful": 3.0, "Questionable": 0.75}


def _can_play(info):
    """Is this player available to actually play for us this season?

    A DEF has neither field and must never be filtered by their absence."""
    if (info.get("injury_status") or "") in UNDRAFTABLE_INJURY:
        return False
    return (info.get("roster_status") or "") not in UNDRAFTABLE_ROSTER


# Adjusted-value distance from the leader within which candidates count as
# "similar". 0.75 fires on ~70% of picks with 2-3 rows tied; tighten it to make
# enrichment rarer. Tuned on evidence, not taste — see tests/draft_replay.py.
# How long to wait for the draft button to enable once we believe it is our
# turn. Comfortably inside any real clock (120s in mocks, 600s on draft day)
# and far longer than the socket needs.
ENABLE_WAIT_TRIES = 20

CLOSE_GAP = 0.75

# OFF BY DEFAULT, on evidence rather than caution. Measured on two real drafts
# with a fixed board, notes on against notes off:
#
#   draft 509312  engine-agreement  8/15 -> 10/15   3 picks changed
#   draft 743168  engine-agreement  8/15 ->  9/15   3 picks changed
#   model latency 3.2s either way; no cost
#
# Agreement with the engine's top pick RISING is the failure signal for this
# layer -- it means the model exercised less judgment, not more -- and the bar
# set before the experiment was "ship only if the picks do not degrade". It
# missed. The changed picks are not obviously worse (Loveland -> Hurts fills an
# empty QB slot and looks like an improvement), but "not obviously worse" is
# not the bar, and 15 picks with no ground truth cannot settle it.
#
# Kept, flagged, and honest: WES_CLOSE_CALL_NOTES=1 to try it. What would
# settle the question is a metric better than agreement -- season points
# scored by the resulting roster, which needs a season.
CLOSE_CALL_NOTES = os.environ.get("WES_CLOSE_CALL_NOTES", "0") == "1"


def _annotate_close_calls(board, index, xwalk, gap=CLOSE_GAP, _now=None):
    """Attach `notes` to the candidates that are too close to separate.

    Returns how many rows were annotated. Mutates in place because the board
    rows are the same dicts the caller returns — copying them here would mean
    two truths about one candidate."""
    if len(board or []) < 2 or not CLOSE_CALL_NOTES:
        return 0
    import wes_notes
    import wes_snapshot
    top = board[0]["adj_value"]
    tied = [c for c in board if top - c["adj_value"] <= gap]
    if len(tied) < 2:
        return 0
    try:
        hist = wes_snapshot.history()
    except Exception:  # noqa: BLE001 — notes are a nicety, never a dependency
        hist = {}
    for c in tied:
        info = index.get(str(c.get("player_key"))) or {}
        espn = info.get("espn_id") or xwalk.get(str(c.get("player_key")))
        seasons = [tuple(x) for x in hist.get(str(espn), [])] if espn else []
        pos = (info.get("positions") or [None])[0]
        team = (info.get("team") or "").strip()
        # Same club, same position — the only players who can be "ahead of" him.
        mates = ([t for t in index.values()
                  if (t.get("team") or "").strip() == team
                  and (t.get("positions") or [None])[0] == pos]
                 if team and pos else [])
        note = wes_notes.notes_for(info, seasons=seasons, teammates=mates,
                                   now=_now)
        if note:
            c["notes"] = note
    return len(tied)


def _handcuff_for(info, my_players):
    """Which player of OURS this candidate backs up, if any.

    A handcuff is the reserve behind a starter we already own: if our man goes
    down, this is who inherits the touches, so he is worth more to US than his
    raw projection says. Returns the starter's name, or None.

    Deliberately computed here rather than described to the model. The join is
    team + position + a strictly lower depth-chart order across two lists, and
    a small model asked to do that in its head produces a confident wrong
    answer — which is exactly what the false bye-week claims looked like."""
    order = info.get("depth_chart_order")
    team = (info.get("team") or "").strip()
    pos = (info.get("positions") or [None])[0]
    if not order or not team or not pos:
        return None
    ahead = [p for p in my_players
             if (p.get("team") or "").strip() == team
             and pos in (p.get("positions") or [])
             and isinstance(p.get("depth_chart_order"), int)
             and p["depth_chart_order"] < order]
    if not ahead:
        return None
    return min(ahead, key=lambda p: p["depth_chart_order"]).get("name")


def _market_rank(raw):
    """Sleeper's search_rank as a real rank, or None if it is a sentinel."""
    if not isinstance(raw, int) or raw <= 0 or raw >= _RANK_SENTINEL:
        return None
    return raw


def players_index(_get_fn=None, _now=None):
    """The slim player index, cached in-process.

    Fetched with ttl=0 so the 14MB raw body is never held in the shared HTTP
    cache — only the parsed index below is kept. Sleeper's docs ask callers not
    to pull this more than once a day; PLAYERS_TTL keeps us well inside that."""
    now = _now if _now is not None else time.time()
    if _players_cache["index"] is not None and \
            now - _players_cache["at"] < PLAYERS_TTL:
        return _players_cache["index"]
    payload = (_get_fn or wes_http.get_json)(f"{BASE}/players/nfl", ttl=0,
                                             timeout=90)
    index = parse_players(payload)
    _players_cache.update(at=now, index=index)
    return index


# --- rosters ----------------------------------------------------------------
def parse_roster(roster, index, slots):
    """One Sleeper roster + the player index + the league's slots -> our
    canonical player dicts. PURE.

    Sleeper models a roster as `starters` (an ORDERED list positionally aligned
    to `roster_positions`) plus `players` (everyone). So a player's slot is
    implied by their INDEX in `starters`, not stored on them — and an empty slot
    is the literal string "0". Anyone in `players` but not in `starters` is on
    the bench."""
    starters = roster.get("starters") or []
    everyone = roster.get("players") or []
    out, seen = [], set()

    for i, pid in enumerate(starters):
        pid = str(pid)
        if pid in ("0", "", "None"):
            continue                      # an unfilled starting slot
        info = index.get(pid) or {}
        out.append({
            "name": info.get("name", f"player {pid}"),
            "positions": info.get("positions") or [],
            "team": info.get("team"),
            "slot": slots[i] if i < len(slots) else "?",
            "status": info.get("status", ""),
            "player_key": pid,
            "espn_id": info.get("espn_id"),
            "gsis_id": info.get("gsis_id"),
        })
        seen.add(pid)

    for pid in everyone:
        pid = str(pid)
        if pid in seen:
            continue
        info = index.get(pid) or {}
        out.append({
            "name": info.get("name", f"player {pid}"),
            "positions": info.get("positions") or [],
            "team": info.get("team"),
            "slot": "BN",
            "status": info.get("status", ""),
            "player_key": pid,
            "espn_id": info.get("espn_id"),
            "gsis_id": info.get("gsis_id"),
        })
    return out


def league(league_id, _get_fn=None):
    return _get(f"/league/{league_id}", _get_fn=_get_fn)


def league_scoring(league_id, _get_fn=None):
    """This league's scoring in our canonical shape."""
    return parse_scoring((league(league_id, _get_fn) or {}).get(
        "scoring_settings"))


def league_slots(league_id, _get_fn=None):
    return parse_roster_slots((league(league_id, _get_fn) or {}).get(
        "roster_positions"))


def rosters(league_id, _get_fn=None):
    return _get(f"/league/{league_id}/rosters", _get_fn=_get_fn)


def roster_players(league_id, roster_id, _get_fn=None, _index_fn=None):
    """The named roster, as canonical player dicts.

    Degrades to a STRING on any problem, matching `wes_yahoo.roster_players` —
    callers upstream relay a degradation verbatim rather than raising into a
    turn, and an adapter that raised where the other returned would break them."""
    try:
        all_rosters = rosters(league_id, _get_fn)
    except Exception as e:  # noqa: BLE001
        return f"I couldn't reach Sleeper to read that roster ({e})."
    if not isinstance(all_rosters, list):
        return "Sleeper returned no rosters for that league."
    want = str(roster_id)
    row = next((r for r in all_rosters if str(r.get("roster_id")) == want), None)
    if row is None:
        return f"No roster {roster_id} in that Sleeper league."
    index = (_index_fn or players_index)()
    return parse_roster(row, index, league_slots(league_id, _get_fn))


def rostered_ids(league_id, _get_fn=None):
    """Every player id owned by ANY team — the complement of the free agents."""
    all_rosters = rosters(league_id, _get_fn)
    if not isinstance(all_rosters, list):
        return set()
    owned = set()
    for r in all_rosters:
        for pid in (r.get("players") or []):
            owned.add(str(pid))
    return owned


def free_agents(league_id, positions=("QB", "RB", "WR", "TE", "K", "DEF"),
                _get_fn=None, _index_fn=None):
    """Everyone in the league's player universe that nobody rosters.

    Sleeper has no "free agents" endpoint — availability is the COMPLEMENT of
    every roster, which is only knowable by reading all of them. That is one
    request, so it is cheaper than Yahoo's paginated scrape, but it does mean
    availability is derived rather than reported."""
    try:
        owned = rostered_ids(league_id, _get_fn)
    except Exception as e:  # noqa: BLE001
        return f"I couldn't reach Sleeper to list free agents ({e})."
    index = (_index_fn or players_index)()
    want = {p.upper() for p in positions}
    out = []
    for pid, info in index.items():
        if pid in owned:
            continue
        if not (set(info.get("positions") or []) & want):
            continue
        out.append({
            "name": info["name"],
            "positions": info.get("positions") or [],
            "team": info.get("team"),
            "status": info.get("status", ""),
            "player_key": pid,
            "espn_id": info.get("espn_id"),
            "gsis_id": info.get("gsis_id"),
            "is_free_agent": True,     # Sleeper waivers are a separate concept
        })
    return out


def user_id(username, _get_fn=None):
    """Sleeper's account id for a display name. None if unknown.

    Needed for MOCK drafts, which have no league at all (`league_id` is null),
    so the roster route cannot be used to find our seat. A mock's `draft_order`
    is keyed by user id, and that is the only place our slot is written down."""
    u = _get(f"/user/{username}", _get_fn=_get_fn)
    return str(u.get("user_id")) if isinstance(u, dict) and u.get("user_id") \
        else None


def slot_in_draft(draft_id, username, _get_fn=None, _draft_fn=None):
    """Which draft SLOT we occupy in an arbitrary draft. None if we are not in
    it.

    Works for mocks and league drafts alike, because it reads `draft_order`
    (user id -> slot) rather than the league's rosters. Sleeper writes this when
    the seat is claimed, which happens on JOINING — so a draft we have not
    joined correctly returns None rather than a guess. Watching the wrong slot
    is not a hypothetical: an early loop sat on slot 1 while our real seat was
    elsewhere and made zero picks (2026-08-15)."""
    uid = user_id(username, _get_fn=_get_fn)
    if not uid:
        return None
    d = (_draft_fn or draft)(draft_id) or {}
    order = d.get("draft_order") or {}
    slot = order.get(uid)
    return int(slot) if slot else None


def find_roster_id(league_id, username, _get_fn=None):
    """Which roster_id belongs to `username`. Returns None if not found.

    Two hops: display_name -> user_id (from /users), then user_id -> roster_id
    (from /rosters). Worth doing once and recording in teams.yaml rather than
    per run — but it is here so registering a league is not a manual id hunt."""
    users = _get(f"/league/{league_id}/users", _get_fn=_get_fn)
    if not isinstance(users, list):
        return None
    want = str(username).strip().lower()
    uid = next((u.get("user_id") for u in users
                if str(u.get("display_name", "")).lower() == want), None)
    if uid is None:
        return None
    all_rosters = rosters(league_id, _get_fn)
    if not isinstance(all_rosters, list):
        return None
    return next((r.get("roster_id") for r in all_rosters
                 if str(r.get("owner_id")) == str(uid)), None)


# --- browser session (writes, #039) -----------------------------------------
# Sleeper's public API is read-only, so anything that CHANGES a team has to go
# through the web app. Mirrors wes_yahoo._Session deliberately: same persistent
# -profile trick, same automation-tell stripping, same context-manager shape, so
# there is one pattern to understand rather than two.
class _Session:
    """A launched persistent browser context holding the Sleeper login.

        with _Session() as page:
            page.goto(...)
    """

    # How many sessions are currently open. Class-level: the limit is per
    # process, not per instance.
    _live = 0

    def __init__(self, headless=None):
        self.headless = HEADLESS if headless is None else headless
        self._pw = None
        self._ctx = None

    def __enter__(self):
        from playwright.sync_api import sync_playwright
        # ONE AT A TIME. Playwright's sync API cannot have two instances live
        # in a thread, and the error it gives -- "Sync API inside the asyncio
        # loop" -- says nothing about the actual mistake. That mistake is easy
        # to make now that a Browser can hold a session for a whole draft: a
        # code path that forgets to accept the held browser opens its own and
        # forfeits a pick (2026-08-21, caught in a full mock).
        if _Session._live:
            raise RuntimeError(
                "a browser session is already open — pass the held "
                "wes_browser.Browser through instead of opening a second one "
                "(Playwright's sync API allows only one per thread)")
        os.makedirs(PROFILE_DIR, exist_ok=True)
        self._pw = sync_playwright().start()
        launch = dict(
            headless=self.headless,
            args=["--disable-blink-features=AutomationControlled"],
            ignore_default_args=["--enable-automation"],
        )
        try:
            self._ctx = self._pw.chromium.launch_persistent_context(
                PROFILE_DIR, channel=BROWSER_CHANNEL or None, **launch)
        except Exception:  # noqa: BLE001 — no Chrome/Edge: bundled Chromium
            self._ctx = self._pw.chromium.launch_persistent_context(
                PROFILE_DIR, **launch)
        page = self._ctx.pages[0] if self._ctx.pages else self._ctx.new_page()
        # ACCEPT NATIVE DIALOGS. Sleeper confirms destructive actions with
        # window.confirm ("Are you sure you want to start the draft? This action
        # cannot be undone"), and Playwright AUTO-DISMISSES native dialogs
        # unless a handler is registered. That is invisible: the click lands,
        # nothing appears in the DOM (a native dialog is not in the DOM), and
        # nothing happens — which was twice misread as "the app ignores
        # synthetic clicks" (2026-08-15).
        #
        # Accepting is right for this module because nothing here clicks
        # anything the caller has not already decided to do: every write is
        # gated by the kill switch, and the guardrails live above this layer,
        # not in a browser prompt.
        page.on("dialog", lambda d: d.accept())
        _Session._live += 1
        return page

    def __exit__(self, *exc):
        try:
            try:
                if self._ctx:
                    self._ctx.close()
            finally:
                if self._pw:
                    self._pw.stop()
        finally:
            # ALWAYS release, even if closing threw. A leaked counter would
            # lock out every later session, which is worse than the bug it
            # guards against.
            _Session._live = max(0, _Session._live - 1)
        return False


def _is_login_wall(url, body):
    """Sleeper bounces a signed-out request to `/?redirect=...&login=` rather
    than showing an error, so a scrape of a logged-out session returns a
    perfectly valid page about something else entirely. Detect it explicitly —
    silently parsing the marketing page would look like an empty roster."""
    return "login=" in (url or "") or "redirect=" in (url or "") \
        or "LOG IN" in (body or "")


def authenticate(page):
    """Put the account token where the web app looks for it.

    Must run with the ORIGIN already loaded — localStorage is per-origin, so
    writing it from about:blank silently lands nowhere. Returns False if no
    token is configured, so callers can say something useful instead of
    presenting an anonymous browser and reporting a mysteriously empty roster."""
    if not TOKEN:
        return False
    page.goto(WEB, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(1500)
    page.evaluate("([k, v]) => window.localStorage.setItem(k, v)",
                  [TOKEN_KEY, TOKEN])
    return True


def logged_in(league_id, _session_cls=None):
    """Is the stored profile still signed in? (bool, detail).

    Never raises: this is the check the owner runs to find out WHY something
    else failed, so it has to survive the failure it is diagnosing."""
    if not TOKEN:
        return False, ("no WES_SLEEPER_TOKEN in the environment — set it and "
                       "restart the process that needs it.")
    try:
        with (_session_cls or _Session)() as page:
            authenticate(page)
            page.goto(f"{WEB}/leagues/{league_id}/team",
                      wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(4000)
            body = " ".join((page.inner_text("body") or "").split())
            if _is_login_wall(page.url, body):
                return False, ("Sleeper bounced to the login page — log in "
                               "once in the browser profile (see #039).")
            return True, f"signed in; team page loaded ({len(body)} chars)"
    except Exception as e:  # noqa: BLE001
        return False, f"couldn't check the Sleeper session: {e}"


# --- draft (#039) -----------------------------------------------------------
# The draft READ path needs no auth at all: picks, order and settings are all
# public. That matters for sequencing — "who is gone, whose turn is it, who
# should I take" is fully solvable today, and only SUBMITTING a pick needs the
# session that hCaptcha is currently blocking.
def draft(draft_id, _get_fn=None):
    return _get(f"/draft/{draft_id}", ttl=60.0, _get_fn=_get_fn)


def draft_status_fresh(draft_id, _get_fn=None):
    """The draft's status, CACHE BYPASSED. For the pre-start wait only.

    `draft()` caches for 60s, and the wait loop also slept 60s -- so polling
    faster would have changed nothing, and the interval looked like the whole
    cause when it was half of it. Arriving 60s late to a 120s clock cost pick 1
    of a live draft, which then engaged autopick and cost the whole draft
    (2026-08-22)."""
    d = _get(f"/draft/{draft_id}", ttl=0, _get_fn=_get_fn)
    return (d or {}).get("status")


def draft_picks(draft_id, _get_fn=None):
    """Every pick made so far, oldest first. Short TTL: during a live draft this
    is the fast-moving fact everything else depends on."""
    return _get(f"/draft/{draft_id}/picks", ttl=15.0, _get_fn=_get_fn)


def join_draft(draft_id, username=None, slot=None, _session_cls=None,
               _slot_fn=None, _sleep_fn=None):
    """Claim a free seat in a draft someone else created. Returns the slot.

    THE BUG, AND WHY IT TOOK TWO INVESTIGATIONS (2026-08-21)

    An empty seat renders as a `.draft-user-header` containing a
    `.header-button` wrapper, which contains `.claim-text` ("CLAIM") and
    `.header-text` ("Team 3"). **The onclick is on `.claim-text`. The wrapper
    has none.**

    Eleven gestures were aimed at the wrapper across two sessions -- Playwright
    click, the header itself, a JS click bypassing hit-testing, the second of
    the two overlapping duplicates, a real mouse click at its centre, a text
    selector, a dispatched pointer sequence, a dispatched touch sequence,
    focus+Enter. Every one was correctly ignored by an element with no handler,
    and each null result sent me looking for a more exotic cause: an overlay,
    React Native Web's responder system, a websocket transport, an
    authorisation state that only real logins get. All wrong.

    What found it in seconds was reading `el.onclick` across the seat's
    descendants instead of theorising about why the click "did not land". The
    lesson is cheap and I paid full price for it: when a click does nothing,
    ask the DOM which element is listening before asking why the event failed.

    THE SECOND HALF: `draft_order` is EVENTUALLY consistent. The seat renders
    as ours immediately, the API can still say nothing for some seconds. The
    original 20s verification window reported failure on a claim that had
    actually succeeded -- so even after the click was fixed, this would have
    looked broken.

    ALREADY IN IT IS A SUCCESS, NOT AN ERROR. Re-running must not claim a
    second seat, so the existing slot is checked first and returned unchanged.

    VERIFIED BY THE API, not by the click. `draft_order` is where the seat is
    actually recorded, and it is read CACHE-BYPASSED afterwards — trusting a
    click that "did not throw" is how a pick was reported successful while the
    draft room had ignored it (2026-08-15)."""
    name = username or USERNAME
    slot_fn = _slot_fn or slot_in_draft
    sleep = _sleep_fn or time.sleep

    have = slot_fn(draft_id, name)
    if have:
        return have
    if not LIVE_WRITES_OK():
        raise RuntimeError("Sleeper writes are off")

    want = f"CLAIM TEAM {slot}" if slot else "CLAIM"
    with (_session_cls or _Session)() as page:
        if not authenticate(page):
            raise RuntimeError("no WES_SLEEPER_TOKEN — cannot reach the draft")
        page.goto(f"{WEB}/draft/nfl/{draft_id}",
                  wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(9000)

        def _labels():
            return [" ".join((h.inner_text() or "").split())
                    for h in page.query_selector_all(".draft-user-header")]

        # ALREADY SEATED? ASK THE DOM, NOT THE API. The cheap check above uses
        # `draft_order`, which lags the claim by over a minute — so a re-run
        # inside that window sailed past it and claimed a SECOND seat. That is
        # the one outcome this function exists to prevent, and the first fix
        # for the lag reintroduced it at the other end.
        held = [i for i, t in enumerate(_labels()) if t.lower() == name.lower()]
        if held:
            return held[0] // 2 + 1

        seat = None
        for h in page.query_selector_all(".draft-user-header"):
            txt = " ".join((h.inner_text() or "").split()).upper()
            if txt.startswith(want):
                # THE HANDLER IS ON `.claim-text`, NOT on the `.header-button`
                # wrapper. That single fact is the whole bug: eleven gestures
                # across two investigations were all aimed at the wrapper,
                # which has no onclick, so every one of them was correctly
                # ignored. Reading `el.onclick` on the seat's descendants
                # found it in seconds after hours of clicking harder.
                seat = (h.query_selector(".claim-text")
                        or h.query_selector(".header-text")
                        or h.query_selector(".header-button"))
                if seat:
                    break
        if seat is None:
            raise RuntimeError(
                f"no free seat matching {want!r} in draft {draft_id} — it is "
                f"either full or the seat labels have changed")
        seat.click()

        # Poll the API rather than the DOM. The seat is ours when Sleeper says
        # so, and only then.
        #
        # PATIENTLY, though: `draft_order` is EVENTUALLY consistent. The seat
        # renders as ours immediately and the API can still say nothing for
        # some seconds afterwards — which is the second half of this bug. The
        # original 20s window reported failure on a claim that had in fact
        # succeeded, and a false failure here invites a human to claim a
        # second seat.
        # THE DOM IS THE AUTHORITY FOR "did the click land", and the API is
        # not. Measured: a claim that renders instantly took over 73 seconds to
        # appear in `draft_order`, so an API-only check reported failure on a
        # seat we already held — and a false failure here invites a second
        # claim, which is the one outcome worth avoiding.
        for _ in range(10):
            page.wait_for_timeout(1500)
            mine = [i for i, t in enumerate(_labels())
                    if t.lower() == name.lower()]
            if mine:
                # Seats render as an adjacent PAIR per team, so the team number
                # is the index halved and one-based.
                return mine[0] // 2 + 1
        raise RuntimeError(
            f"clicked the free seat in draft {draft_id} but no seat shows "
            f"{name} — the claim did not take")


# The chat lives behind a tab in the draft room and has NO public endpoint —
# every /draft/<id>/chat path 404s — so it is DOM or nothing.
_CHAT_AGO = re.compile(r"^\d+\s+(second|minute|hour|day|week)s?\s+ago$", re.I)


def _chat_rows(page):
    """Scrape the open chat panel. Reads the page, changes nothing.

    Sleeper renders each message as `.message-text` inside a container whose
    text begins with the AUTHOR, then a relative timestamp, then "reply".
    System messages ("X has joined the draft!") have no author line at all,
    which is exactly how we tell them apart — and not replying to the server
    announcing that someone joined is most of the value."""
    return page.evaluate("""() => {
        const out = [];
        for (const m of document.querySelectorAll('.message-text')) {
            let p = m, hops = 0, ctx = '';
            while (p && hops < 5) {
                p = p.parentElement; hops++;
                if (p && (p.innerText || '').length > (m.innerText || '').length) {
                    ctx = p.innerText; break;
                }
            }
            out.push({text: (m.innerText || '').trim(),
                      context: (ctx || '').split('\\n').slice(0, 2)});
        }
        return out;
    }""")


def parse_chat(rows):
    """Rows -> [{author, text, system}]. PURE, so the parser is testable
    without a browser."""
    out = []
    for r in rows or []:
        text = (r.get("text") or "").strip()
        if not text:
            continue
        # Accept a STRING or a list of lines. Iterating a bare string yields
        # CHARACTERS, which silently produced authors like "a" and "2" -- a
        # wrong answer wearing the shape of a right one.
        ctx = r.get("context") or []
        if isinstance(ctx, str):
            ctx = ctx.splitlines()
        author = ""
        for line in [str(x).strip() for x in ctx]:
            if line and line.lower() != "reply" and not _CHAT_AGO.match(line):
                author = line
                break
        # A SYSTEM message has no author line, so the first non-timestamp line
        # is the message itself — and taking that as the author made the server
        # look like a participant called "aykutb has joined the draft!". If the
        # two match, nobody said it.
        if author and text.startswith(author):
            author = ""
        out.append({"author": author, "text": text, "system": not author})
    return out


def _show_chat(page):
    """Bring the CHAT tab forward on a page ALREADY in the draft room.

    Idempotent: if the panel is up we leave it, so a held-open session does not
    pay a tab click and a 3s settle on every read."""
    if page.query_selector("textarea[placeholder='Enter Message']"):
        return
    tab = next((t for t in page.query_selector_all(".round-tab, .tab")
                if " ".join((t.inner_text() or "").split()).upper() == "CHAT"),
               None)
    if tab is None:
        raise RuntimeError("no CHAT tab in the draft room")
    tab.click()
    page.wait_for_timeout(2000)


def _open_chat(page, draft_id):
    page.goto(f"{WEB}/draft/nfl/{draft_id}", wait_until="domcontentloaded",
              timeout=60000)
    page.wait_for_timeout(9000)
    _show_chat(page)


def read_chat(draft_id, _session_cls=None, browser=None):
    """Every message in the draft chat, oldest first. Read-only.

    `browser` is an optional wes_browser.Browser holding a page open for the
    draft. Reading the chat cost a full browser launch every time -- ~9s, paid
    on almost every poll of the banter loop -- and with a held page it is
    sub-second. Without one it behaves exactly as before."""
    if browser is not None:
        page = browser.page()
        _show_chat(page)
        return parse_chat(_chat_rows(page))
    with (_session_cls or _Session)() as page:
        page.set_viewport_size({"width": 1600, "height": 1000})
        if not authenticate(page):
            raise RuntimeError("no WES_SLEEPER_TOKEN — cannot reach the draft")
        _open_chat(page, draft_id)
        return parse_chat(_chat_rows(page))


def send_chat(draft_id, text, _session_cls=None, browser=None):
    """Post one message. True once it is VISIBLE in the panel afterwards.

    Verified by re-reading, like every other write here: `join_draft` clicked a
    live-looking control six different ways and fired no network request at
    all, so "the click did not throw" means nothing on this site."""
    body = " ".join((text or "").split())
    if not body:
        return False
    if not LIVE_WRITES_OK():
        raise RuntimeError("Sleeper writes are off")
    if browser is not None:
        page = browser.page()
        _show_chat(page)
        return _post_message(page, body)
    with (_session_cls or _Session)() as page:
        page.set_viewport_size({"width": 1600, "height": 1000})
        if not authenticate(page):
            raise RuntimeError("no WES_SLEEPER_TOKEN — cannot reach the draft")
        _open_chat(page, draft_id)
        return _post_message(page, body)


def _post_message(page, body):
    """Type and send, then confirm it is visible. Shared by both paths so the
    held-open session cannot quietly diverge from the per-call one."""
    box = page.query_selector("textarea[placeholder='Enter Message']")
    if box is None:
        raise RuntimeError("no message box in the chat panel")
    box.click()
    box.fill(body)
    page.keyboard.press("Enter")
    page.wait_for_timeout(2500)
    return any(m["text"] == body for m in parse_chat(_chat_rows(page)))


def drafted_player_ids_fresh(draft_id, _get_fn=None):
    """Taken ids, CACHE BYPASSED — for the check made immediately before a pick.

    `draft_picks` holds a 15s TTL, which is fine for polling and fatal here:
    the loop's "re-check availability before submitting" rail was reading a
    pick list up to 15 seconds old, so a player taken moments earlier still
    looked available. That is exactly how a pick was wasted on Ka'imi
    Fairbairn, who had already been drafted (2026-08-15). Verifying against a
    cache is not verifying — the same lesson as the post-write check."""
    picks = _get(f"/draft/{draft_id}/picks", ttl=0, _get_fn=_get_fn)
    if not isinstance(picks, list):
        return set()
    return {str(p.get("player_id")) for p in picks if p.get("player_id")}


def drafted_player_ids(draft_id, _get_fn=None):
    """Ids already taken by ANYONE.

    Matched by player_id, never by name: Sleeper hands us the exact id on every
    pick, and name matching is how a draft bot recommends someone who is already
    gone (two players share a name, or a suffix differs)."""
    picks = draft_picks(draft_id, _get_fn)
    if not isinstance(picks, list):
        return set()
    return {str(p.get("player_id")) for p in picks if p.get("player_id")}


def _draft_pool():
    """The valuation pool for a DRAFT: forward-looking projections, with last
    season's actuals as a fallback.

    Kept separate from the in-season pool on purpose. The two answer different
    questions — "what will this player do this year" vs "what has this player
    been doing" — and quietly swapping one for the other is how a draft board
    ends up confidently out of date."""
    import wes_snapshot
    proj = wes_snapshot.projections()      # snapshot first, live fetch if none
    if not proj:
        pool, _failed = wes_nfl.pool_by_position()
        return pool

    # K and DEF are FETCHED in the projection feed (32 of each) but dropped by
    # parse_projections, because kicking and defensive statIds are not mapped —
    # so their `cats` come back empty. A league with K and DEF slots cannot
    # draft either position from a projections-only pool.
    #
    # Interim: fill those two positions from last season's ACTUALS. It mixes
    # bases, which is normally exactly the kind of quiet wrongness worth
    # refusing — but replacement level is computed WITHIN a position, so a
    # kicker is still compared against other kickers. K and DEF are also
    # low-variance and late-round, which is the one place this compromise costs
    # least. The real fix is mapping their statIds, same inference-and-validate
    # exercise as the offensive ones (#039).
    have = {(pl.get("positions") or [None])[0] for pl in proj}
    missing = {"K", "DEF"} - have
    if missing:
        actuals, _failed = wes_nfl.pool_by_position()
        proj = list(proj) + [a for a in actuals
                             if (a.get("positions") or [None])[0] in missing]
    return proj


def draft_candidates(league_id, draft_id, roster_id, limit=8, _get_fn=None,
                     _index_fn=None, _pool_fn=None, _picks=None):
    """Live draft STATE + verified candidates, as STRUCTURED DATA.

    Split from the prose version so the draft agent has something to reason
    over. An agent handed a formatted string would have to parse back out
    what this already knows — and per docs/data-architecture.md the decision
    layer sits BELOW the model layer, so the data must exist without the
    words. `draft_board` formats this.

    Every candidate returned is verified available BY ID, legal under the
    hard same-team cap, and actually valued — which is what makes it safe to
    let a model choose freely among them (#039).

    Everything here is READ-ONLY and needs no login — the whole recommendation
    path works today even with the write session blocked by hCaptcha. With a
    10-minute pick timer, a recommendation relayed to a human is most of the
    value of an autodrafter, and `cpu_autopick` is the safety net if nobody is
    watching.

    Degrades to a string on any problem; never raises into a turn."""
    import wes_draft
    try:
        d = draft(draft_id, _get_fn)
        # `_picks` lets a caller supply a HISTORICAL pick prefix, so the board
        # can be reconstructed as it stood at an earlier moment. That is what
        # makes a completed draft replayable — otherwise every question about
        # "what would we have done at pick 37" needs a live draft to answer.
        picks = _picks if _picks is not None else draft_picks(draft_id, _get_fn)
    except Exception as e:  # noqa: BLE001
        return f"I couldn't reach Sleeper's draft API ({e})."
    if not isinstance(d, dict):
        return "Sleeper returned no draft for that id."
    picks = picks if isinstance(picks, list) else []

    settings = d.get("settings") or {}
    teams = int(settings.get("teams") or 0)
    rounds = int(settings.get("rounds") or 0)
    reversal = int(settings.get("reversal_round") or 0)
    # slot_to_roster_id maps DRAFT SLOT -> roster; we need the inverse, because
    # the caller knows their roster, not which seat it sits in.
    slot_of = {str(v): int(k)
               for k, v in (d.get("slot_to_roster_id") or {}).items()}
    my_slot = slot_of.get(str(roster_id))
    if not (teams and rounds and my_slot):
        return ("That Sleeper draft hasn't published its order yet — no slot "
                "assignment to reason from.")

    made = len(picks)
    wait = wes_draft.picks_until_turn(my_slot, teams, made, rounds, reversal)
    if wait is None:
        return f"That draft is over — all {made} picks are in."

    taken = {str(p.get("player_id")) for p in picks if p.get("player_id")}
    # Identify MY picks by DRAFT SLOT, not roster_id. A real mock draft returns
    # `roster_id: null` on every pick (there is no league behind it), so keying
    # on roster_id silently found none of my players and the need bump was
    # computed against an empty roster — found 2026-08-15 by reading a live
    # pick record rather than by testing. roster_id stays as a fallback for
    # league drafts, where it is populated.
    mine = [str(p.get("player_id")) for p in picks
            if p.get("player_id")
            and (p.get("draft_slot") == my_slot
                 or (p.get("roster_id") is not None
                     and str(p.get("roster_id")) == str(roster_id)))]

    import wes_snapshot
    index = (_index_fn or wes_snapshot.players)()
    _xwalk = wes_snapshot.crosswalk()
    scoring = league_scoring(league_id, _get_fn)
    slots = league_slots(league_id, _get_fn)
    targets, flex, flex_pos = wes_draft.targets_from_slots(slots)

    have = {}
    my_players = []
    for pid in mine:
        info = index.get(pid) or {}
        my_players.append({"team": info.get("team"),
                           "positions": info.get("positions") or [],
                           "name": info.get("name"),
                           "depth_chart_order": info.get("depth_chart_order")})
        for pos in info.get("positions") or []:
            have[pos] = have.get(pos, 0) + 1

    # Bye weeks come from the SCHEDULE — no fantasy platform we read supplies
    # one. Degrades to {} (unknown), never to "no bye".
    byes = wes_snapshot.byes()

    # Value the AVAILABLE players by joining Sleeper ids to the ESPN pool on
    # espn_id — an exact join, not a name match.
    # SEASON PROJECTIONS, not last year's actuals. A draft is about expected
    # season-long production; valuing on 2025 results is why the board kept
    # recommending last year's producers a hundred picks after the market had
    # moved on (Tyreek Hill on top at pick 146, 2026-08-15). Falls back to
    # actuals rather than emptying the board if projections are unavailable.
    pool = (_pool_fn or _draft_pool)()
    # Join by espn_id FIRST — exact — then fall back to normalised name+position.
    #
    # The id join alone silently hid most of the board: Sleeper leaves `espn_id`
    # null for a great many players, including Gibbs, Nacua, Bijan Robinson and
    # Chase, so only 91 of 324 projections were reachable and 13 of the top 25
    # projected players could never be drafted (measured 2026-08-15). A skipped
    # player is not ranked low, he is ABSENT — which is why the drafts looked
    # plausible while the best players were missing.
    #
    # Position is part of the name key so a shared surname cannot cross-join a
    # receiver onto a linebacker's projection.
    by_espn = {str(p["espn_id"]): p for p in pool if p.get("espn_id")}
    by_name = {}
    for sp in pool:
        key = (_norm_name(sp.get("name")), (sp.get("positions") or [None])[0])
        by_name.setdefault(key, sp)
    board = []
    for pid, info in index.items():
        if pid in taken:
            continue
        # espn_id from Sleeper, else from the crosswalk, else name+position.
        # The crosswalk is the one that actually covers modern players.
        espn = info.get("espn_id") or _xwalk.get(str(pid))
        stat = by_espn.get(str(espn)) if espn else None
        if stat is None:
            stat = by_name.get((_norm_name(info.get("name")),
                                (info.get("positions") or [None])[0]))
        if stat is None and (info.get("positions") or [None])[0] == "DEF":
            # The two feeds name defences differently and neither is wrong:
            # ESPN uses the NICKNAME ("Texans"), Sleeper's UI the full club
            # ("Houston Texans"). The UI spelling is the one we must click, so
            # it wins for `name` and the join adapts here instead.
            nick = (info.get("name") or "").split()[-1:] or [""]
            stat = by_name.get((_norm_name(nick[0]), "DEF"))
        if stat is None:
            continue                      # unvalued: never recommend a guess
        pos = (info.get("positions") or [None])[0]
        val = wes_nfl.fantasy_points(wes_nfl.per_game(stat).get("cats"),
                                     scoring["weights"], scoring["tiers"])
        if not _can_play(info):
            # HARD EXCLUSION, alongside the same-team cap — not a penalty the
            # model may weigh. This league has 15 roster spots and NO IR slot,
            # so a player who cannot take the field occupies a bench seat all
            # season for nothing. 23 players sit on IR and 40 are Inactive, and
            # until now every one of them was on the board at full projected
            # value with nothing marking them (2026-08-15).
            continue
        board.append({"name": info["name"], "positions": info.get("positions"),
                      "team": info.get("team"), "player_key": pid,
                      # Market price. Lets the model reason about reaching and
                      # about who survives to the next turn; None means
                      # unranked, which is NOT the same as ranked last.
                      "market_rank": info.get("search_rank"),
                      "injury": info.get("injury_status") or None,
                      "depth_chart_order": info.get("depth_chart_order"),
                      # COMPUTED, not left for the model to infer. "You hold
                      # Barkley and this is the PHI RB2" is a checkable fact;
                      # asking a 12b to join team+position+depth across two
                      # lists is how you get a confident wrong answer.
                      "handcuff_for": _handcuff_for(info, my_players),
                      # The candidate's own bye. The ROSTER already carried one
                      # and the candidates did not, so the model could see the
                      # weeks it was already exposed on but not which pick would
                      # make that worse — half a comparison is not one.
                      "bye": byes.get((info.get("team") or "").strip()),
                      "value": round(val, 2)})
    if not board:
        return "I couldn't value any available player for that draft."

    # Rank by VALUE OVER REPLACEMENT, not raw points. Raw points put six QBs in
    # the top eight of this very board, because a quarterback out-scores a back
    # while being far easier to replace. Then add the roster-need bump on top.
    repl = wes_draft.replacement_levels(board, targets, flex, flex_pos, teams)
    fitted = []
    for p in board:
        pos = (p["positions"] or [None])[0]
        # Value says who is BEST; fit says who belongs on THIS roster. A board
        # that knows only value hands you three Bengals and four players on the
        # week-9 bye, and is arithmetically right while losing you the week.
        allowed, penalty, why = wes_draft.roster_fit(p, my_players, byes)
        if not allowed:
            continue                       # hard cap: excluded, not discouraged
        p["vor"] = round(p["value"] - repl.get(pos, 0.0), 2)
        gap = max(0, targets.get(pos, 0) - have.get(pos, 0))
        # Filling a gap is a bonus; OVER-filling has to be a cost. Without
        # this the bump merely goes to zero once a position is satisfied, so a
        # tenth running back still outranks a first quarterback whenever RBs
        # have the higher raw VOR — which is exactly what happened: nine
        # consecutive RBs and no QB, TE, K or DEF (2026-08-15, full mock).
        #
        # `startable` is how many of this position can actually take the field;
        # one backup beyond that is reasonable, and every one after that is a
        # bench flyer competing against a starting slot nobody has filled.
        startable = targets.get(pos, 0) + (flex if pos in flex_pos else 0)
        surplus = have.get(pos, 0) - (startable + 1)
        # An unfilled slot is not equally urgent at every position. Once the
        # skill slots were full, K and DEF were the only gaps left and took the
        # full bump, which put a KICKER on the board in round 7 — real drafters
        # take them last because replacement level there is nearly flat, so the
        # 1st kicker is barely better than the 12th. Scale their urgency down.
        urgency = 0.25 if pos in ("K", "DEF") else 2.0
        p["need_bump"] = (urgency * gap if gap else
                          (0.5 if (pos in flex_pos and flex) else 0.0))
        if surplus > 0:
            p["need_bump"] -= 2.5 * surplus
        # Injury risk costs VALUE and is NAMED, rather than removing the player.
        # A hard exclusion here would have deleted a top-12 tight end over a
        # camp designation (see RISKY_INJURY).
        hurt = p.get("injury")
        if hurt in RISKY_INJURY:
            penalty += INJURY_PENALTY.get(hurt, 1.0)
            why = list(why) + [f"listed {hurt}"]
        p["fit_penalty"] = penalty
        p["fit_reasons"] = why
        p["adj_value"] = round(p["vor"] + p["need_bump"] - penalty, 2)
        fitted.append(p)
    board = fitted or board
    board.sort(key=lambda x: x["adj_value"], reverse=True)

    # The shortlist must offer a real CHOICE. Top-N by adj_value alone was
    # positionally monotone — at picks 28, 48, 68 and 73 it was eight running
    # backs and nothing else, and no quarterback appeared on any shortlist all
    # draft. "The model decided" is not true when the engine has already
    # narrowed the options to one position; it is the engine deciding, with a
    # model rubber-stamping. So: the best few overall, PLUS the best available
    # at every position, so roster construction is actually on the table.
    best_overall = board[:max(1, limit - 3)]
    seen = {id(c) for c in best_overall}
    per_pos = []
    for want in ("QB", "RB", "WR", "TE", "K", "DEF"):
        pick = next((c for c in board
                     if (c["positions"] or [None])[0] == want
                     and id(c) not in seen), None)
        if pick is not None:
            per_pos.append(pick)
            seen.add(id(pick))
    board = sorted(best_overall + per_pos,
                   key=lambda x: x["adj_value"], reverse=True)

    # CLOSE CALLS GET DETAIL, and only close calls. Enriching every row made
    # the local model measurably worse (see the frozen note in
    # wes_draft_agent) — but when the top candidates sit within a rounding
    # error of each other the engine has no opinion left to protect, and a
    # career arc or a depth-chart role is the tiebreak a human would reach
    # for. Measured on two real drafts: fires on ~70% of picks, but on 2-3
    # rows rather than all eleven.
    #
    # If it destabilises the model it does so precisely where being wrong
    # costs least, which is what makes it worth trying at all.
    tied = _annotate_close_calls(board, index, _xwalk)

    unfilled = {pos: max(0, n - have.get(pos, 0))
                for pos, n in targets.items()
                if max(0, n - have.get(pos, 0)) > 0}
    # POSITIONAL RUNS, from picks we already hold. The prompt has told the model
    # to "consider positional runs" since the first version while handing it no
    # picks to see one in — the identical omission as the bye weeks, still live
    # in a second place until now (2026-08-15). A run is the whole reason to
    # take a position early: if six of the last ten picks were RBs, the tier
    # you are looking at will not survive your next turn.
    recent = {}
    for p in picks[-RUN_WINDOW:]:
        rp = ((p.get("metadata") or {}).get("position")
              or (index.get(str(p.get("player_id"))) or {}).get("positions")
              or [None])
        rp = rp if isinstance(rp, str) else (rp[0] if rp else None)
        if rp:
            recent[rp] = recent.get(rp, 0) + 1
    bye_counts = {}
    for p in mine:
        wk = byes.get(((index.get(p) or {}).get("team") or "").strip())
        if wk is not None:
            bye_counts[str(wk)] = bye_counts.get(str(wk), 0) + 1

    return {
        "round": made // teams + 1, "picks_made": made,
        "picks_until_turn": wait, "on_the_clock": wait == 0,
        "my_slot": my_slot, "teams": teams, "roster_size": len(mine),
        # The ROSTER and what is still unfilled — the model cannot reason about
        # roster construction from a shortlist alone, and passing only the
        # shortlist is why it took nine running backs without noticing.
        "roster": [{"name": (index.get(p) or {}).get("name"),
                    "position": ((index.get(p) or {}).get("positions")
                                 or [None])[0],
                    "team": (index.get(p) or {}).get("team"),
                    "bye": byes.get((index.get(p) or {}).get("team"))}
                   for p in mine],
        "starting_slots": list(slots),
        "still_unfilled": unfilled,
        # BYE EXPOSURE, counted rather than described. roster_fit already
        # penalises clustering, but that penalty is a number folded into
        # adj_value and the model never saw the weeks themselves — it was asked
        # to "consider bye-week spread" with no bye weeks in front of it.
        "bye_counts": bye_counts,
        # What the room has been taking lately, so a run is visible as a run.
        "recent_picks_by_position": recent,
        # How many candidates the engine could not separate. Recorded so a
        # later review can ask whether the model earned its keep on the picks
        # that were genuinely close, rather than on the ones that were not.
        "tied_count": tied,
        # WHICH DRAFT WE ARE IN. Filling starters and building a bench are
        # different problems, and need-based reasoning has nothing left to say
        # once every slot is full: the last seven picks of a clean mock were all
        # RBs justified as "highest VOR to bolster RB depth", five times in the
        # same words (2026-08-15). Naming the phase gives the model something to
        # reason WITH after need runs out.
        "phase": "starters" if unfilled else "depth",
        "candidates": board,
    }


def draft_board(league_id, draft_id, roster_id, limit=8, **kw):
    """The same state as prose, for a human or a chat reply."""
    out = draft_candidates(league_id, draft_id, roster_id, limit=limit, **kw)
    if isinstance(out, str):
        return out
    when = ("you're ON THE CLOCK" if out["on_the_clock"]
            else f"{out['picks_until_turn']} picks until your turn")
    lines = [f"Round {out['round']}, {out['picks_made']} picks in — {when} "
             f"(slot {out['my_slot']} of {out['teams']}).", "Best available:"]
    for i, p in enumerate(out["candidates"], 1):
        pos = "/".join(p["positions"] or []) or "?"
        need = f" +{p['need_bump']:g} need" if p.get("need_bump") else ""
        fit = "; ".join(p.get("fit_reasons") or [])
        fit = f" [-{p['fit_penalty']:g}: {fit}]" if fit else ""
        lines.append(f"  {i}. {p['name']} ({pos}, {p['team']}) — "
                     f"{p['vor']:g} over replacement "
                     f"({p['value']:g} pts/g){need}{fit}")
    return "\n".join(lines)


# The AUTO-PICK toggle. Sleeper turns this ON BY ITSELF the moment you miss a
# pick, and it STAYS on until somebody clicks it off — so one missed pick does
# not cost one pick, it costs the rest of the draft. That is what happened on
# 2026-08-21: pick 1 was lost to a disabled button, autopick engaged, and every
# later "pick" was Sleeper choosing instantly while our clicks landed on
# nothing. The loop reported success throughout, because the player it wanted
# had indeed been drafted — by somebody.
#
# Structure mirrors the claim seat: the state is a hidden checkbox and the
# handler is on the `span.slider` beside it, not on any of the wrappers.
_AUTOPICK_BOX = ".autopick-toggle-container input[type=checkbox]"
_AUTOPICK_SLIDER = ".autopick-toggle-container span.slider"


def autopick_on(page):
    """Is our seat set to draft automatically? None if the control is absent."""
    return page.evaluate(
        "(sel) => { const c = document.querySelector(sel);"
        " return c ? !!c.checked : null; }", _AUTOPICK_BOX)


def set_autopick(page, on=False, _tries=6):
    """Force AUTO-PICK to `on`. Returns the state we ended up in.

    Verified by re-reading the checkbox, not by the click landing: this is a
    control whose whole purpose is to act instead of us, so believing a click
    we cannot confirm is the worst possible place to be optimistic."""
    for _ in range(_tries):
        cur = autopick_on(page)
        if cur is None or cur == on:
            return cur
        slider = page.query_selector(_AUTOPICK_SLIDER)
        if slider is None:
            return cur
        slider.click()
        page.wait_for_timeout(700)
    return autopick_on(page)


def _click_pick(page, player_name, want):
    """Find the player's ROW and click its draft control.

    Shared by the per-call and held-open paths. Moved here verbatim
    rather than duplicated: a fix applied to one copy and not the
    other is precisely how the held-open path would diverge from the
    behaviour that is actually tested."""
    # WAIT FOR THE LIST TO EXIST, don't assume a fixed 6s is enough. An
    # unrendered list and an absent player look identical to a query
    # selector, and reading the first as the second cost a pick: Amon-Ra St.
    # Brown was declared gone on the run's first (slowest) page load and
    # taken by us one pick later, so he had plainly been there all along
    # (2026-08-15).
    try:
        page.wait_for_selector(".player-rank-item2", timeout=25000)
    except Exception:  # noqa: BLE001 — turned into a clearer error below
        pass
    page.wait_for_timeout(1500)   # let the rest of the window paint

    def _find_row():
        for r in page.query_selector_all(".player-rank-item2"):
            nm = r.query_selector(".name-wrapper")
            first = (nm.inner_text() or "").split("\n")[0] if nm else ""
            if nm and _norm_name(first) == want:
                return r
        return None

    row = _find_row()
    if row is None:
        # THE LIST IS WINDOWED — roughly 59 rows render at a time, ordered
        # by Sleeper's own ranking. A player we rate highly can sit far
        # outside that window and be perfectly available while simply not
        # drawn: a kicker failed seven times in one draft for exactly this,
        # never having been taken at all (2026-08-15). The search box is how
        # a human reaches him, so use it before concluding anything.
        box = page.query_selector("input[placeholder*='Find player']")
        if box is not None:
            box.click()
            box.fill(player_name)
            page.wait_for_timeout(2500)
            row = _find_row()
    if row is None:
        # TWO CAUSES, and only one of them means "pick someone else". If the
        # list is empty the draft room never rendered and we know NOTHING
        # about this player — saying "he is gone" there is a guess, and the
        # caller substitutes on it.
        if not page.query_selector_all(".player-rank-item2"):
            raise RuntimeError(
                "the draft room's player list never rendered — cannot tell "
                "whether anyone is available; refusing to pick blind")
        raise RuntimeError(
            f"{player_name!r} is not available in the draft room even "
            f"after searching — he has most likely just been taken. "
            f"Refusing to click a different row")

    btn = row.query_selector(".draft-button")
    if btn is None:
        raise RuntimeError(f"no draft control on {player_name}'s row")
    if "disable" in (btn.get_attribute("class") or ""):
        # WAIT FOR IT TO ENABLE, do not sample it once. The button is disabled
        # until the room has been told over its socket that it is our turn, and
        # a page loaded seconds earlier has not heard yet. Checking once cost
        # pick 1 of a live draft to cpu_autopick while it genuinely WAS our
        # turn (2026-08-21).
        #
        # The refusal below still stands, and still matters -- clicking a
        # disabled control reports a pick that never happened. This only
        # distinguishes "not yet" from "not ours", which is the same
        # unrendered-versus-absent distinction that has bitten twice already.
        for _ in range(ENABLE_WAIT_TRIES):
            page.wait_for_timeout(1000)
            btn = row.query_selector(".draft-button") or btn
            if "disable" not in (btn.get_attribute("class") or ""):
                break
        else:
            # TRY ANYWAY. This refusal made sense when a click that landed on
            # nothing could be mistaken for success -- but verification now
            # requires the pick's draft_slot to be OURS, so a no-op click
            # fails honestly a few seconds later.
            #
            # And the class is not trustworthy: on 2026-08-22, with 37 picks
            # made, pick 38 ours, and autopick confirmed OFF, the button still
            # read `disable` after twenty seconds. Refusing there forfeited a
            # pick we were entitled to make. A click that might work beats a
            # certainty of standing down.
            print(f"[sleeper] draft button still reads disabled after "
                  f"{ENABLE_WAIT_TRIES}s — clicking anyway; verification will "
                  f"catch it if it does nothing", flush=True)
    btn.scroll_into_view_if_needed()
    page.wait_for_timeout(300)
    btn.click()
    page.wait_for_timeout(1500)

    # START DRAFT opens a confirmation modal; assume this does too rather
    # than rediscovering it the expensive way (2026-08-15).
    for sel in ("button:has-text('Confirm')", "button:has-text('Draft')",
                "[class*='confirm'] button", "[class*='modal'] button"):
        el = page.query_selector(sel)
        if el and el.is_visible():
            el.click()
            break
    page.wait_for_timeout(3000)


def submit_pick(draft_id, player_key, player_name, _session_cls=None,
                _picks_fn=None, _sleep_fn=None, browser=None, slot=None):
    """Draft ONE player in the live draft room. Raises on any doubt.

    THE ID/NAME SEAM, and why this verifies afterwards. The engine reasons in
    `player_id` — exact, unambiguous, the id Sleeper itself hands us on every
    pick. **But the draft room's DOM contains no player id anywhere**: a row is
    `div.player-rank-item2` holding a `.name-wrapper` with a display name and a
    per-row `.draft-button`. So the click has to be matched by NAME, and names
    are the ambiguous thing (suffixes, punctuation, two players sharing one).

    That gap is closed the only honest way — by reading the pick back from the
    API and confirming the id that actually got drafted is the id we intended.
    Same discipline as `_submit_lineup` and `_submit_add_drop`: never assume a
    click did what it looked like.

    Note EVERY row has its own `.draft-button`, so "click the draft button"
    would draft whoever happens to sit at the top of a re-sorting list. That is
    the Yahoo swap bug wearing a different hat, and it is why this targets the
    ROW belonging to a named player and never a bare control.

    NOT YET EXERCISED AGAINST A LIVE CLOCK — the button carries a `disable`
    class until it is your turn, and the mock finished before one came round.
    """
    if not LIVE_WRITES_OK():
        raise RuntimeError("Sleeper writes are off")

    want = _norm_name(player_name)
    if browser is not None:
        # A held page is already in the draft room, but RELOAD it first: the
        # available list is the one thing a long-lived page cannot be trusted
        # about, and picking off a stale list is the whole class of bug this
        # module keeps paying for.
        _click_pick(browser.refresh(), player_name, want)
    else:
        with (_session_cls or _Session)() as page:
            if not authenticate(page):
                raise RuntimeError(
                    "no WES_SLEEPER_TOKEN — cannot reach the draft")
            page.goto(f"{WEB}/draft/nfl/{draft_id}",
                      wait_until="domcontentloaded", timeout=60000)
            _click_pick(page, player_name, want)

    # Verify by ID — the only thing that closes the name-matching gap.
    #
    # POLLED, and UNCACHED. Two ways the first version got this wrong, both of
    # which reported failure on a pick that had actually succeeded (2026-08-15,
    # live): Sleeper takes a moment to commit, so a single read ~3s after the
    # click can legitimately miss it; and `draft_picks` caches for 15s, so the
    # verification could be served the pre-write answer — verifying against a
    # cache is not verifying at all.
    #
    # A false "did it work?" is worse here than a slow yes: it invites a human
    # into a live draft to fix something that is not broken, and the obvious
    # fix (pick again) drafts twice.
    for attempt in range(6):
        if attempt:
            (_sleep_fn or time.sleep)(2.5)
        picks = (_picks_fn or _draft_picks_uncached)(draft_id) or []
        # BY US, not by anyone. The first version asked only whether the player
        # appeared in the pick list at all, so when our click missed and
        # ANOTHER MANAGER took him two picks later, it read their pick as ours
        # and reported success. Observed live: the log said "DRAFTED Trey
        # McBride" while McBride went to slot 3 at pick 23 and our pick 21 was
        # Lamar Jackson (2026-08-21).
        #
        # This is the check that was supposed to close the name-matching gap,
        # and a check that can be satisfied by someone else's action closes
        # nothing.
        for pk in picks:
            if str(pk.get("player_id")) != str(player_key):
                continue
            if slot is None or int(pk.get("draft_slot") or -1) == int(slot):
                return True
            raise RuntimeError(
                f"{player_name!r} was drafted, but by slot "
                f"{pk.get('draft_slot')} at pick {pk.get('pick_no')} — not by "
                f"us (slot {slot}). Our click did not land.")
    raise RuntimeError(
        f"clicked {player_name!r} but {player_key} never appeared in the "
        f"draft's picks after ~15s — check the draft room before assuming "
        f"either way")


def _draft_picks_uncached(draft_id):
    """Picks with the cache bypassed, for post-write verification only."""
    return _get(f"/draft/{draft_id}/picks", ttl=0)


def _norm_name(s):
    """Loose name key: case, punctuation and suffixes removed, so 'A.J. Brown'
    matches 'AJ Brown' and 'Marvin Harrison Jr.' matches 'Marvin Harrison Jr'."""
    s = (s or "").lower()
    for junk in (".", ",", "'", "-", " jr", " sr", " ii", " iii", " iv"):
        s = s.replace(junk, "")
    return " ".join(s.split())


def LIVE_WRITES_OK():
    """Sleeper writes share the fantasy kill switch rather than inventing a
    second one — one switch to reason about, and #029's `fantasy_live_writes`
    already surfaces at GET /health."""
    import wes_execute
    return wes_execute.writes_enabled()


def draft_turn(draft_id, roster_id, _get_fn=None):
    """CHEAP "is it my turn?" — two small API reads, no valuation.

    Split from `draft_candidates` because the loop has to poll FAST and that
    function is expensive: it pulls the ESPN projection pool and the 12k-player
    index every call. Polling with it meant each look cost seconds, so a draft
    moving at ~4s per pick was over before the loop ever saw its own turn
    (observed live 2026-08-15: 65 picks elapsed, 0 taken).

    The expensive board is built ONLY once this says we are close."""
    try:
        d = draft(draft_id, _get_fn)
        picks = _get(f"/draft/{draft_id}/picks", ttl=3.0, _get_fn=_get_fn)
    except Exception as e:  # noqa: BLE001
        return f"I couldn't reach Sleeper's draft API ({e})."
    if not isinstance(d, dict):
        return "Sleeper returned no draft for that id."
    if d.get("status") == "complete":
        return "That draft is over."
    picks = picks if isinstance(picks, list) else []

    settings = d.get("settings") or {}
    teams = int(settings.get("teams") or 0)
    rounds = int(settings.get("rounds") or 0)
    reversal = int(settings.get("reversal_round") or 0)
    slot_of = {str(v): int(k)
               for k, v in (d.get("slot_to_roster_id") or {}).items()}
    my_slot = slot_of.get(str(roster_id))
    if not (teams and rounds and my_slot):
        return "That draft hasn't published its order yet."

    import wes_draft
    made = len(picks)
    wait = wes_draft.picks_until_turn(my_slot, teams, made, rounds, reversal)
    return {"picks_made": made, "picks_until_turn": wait,
            "on_the_clock": wait == 0, "my_slot": my_slot, "teams": teams}
