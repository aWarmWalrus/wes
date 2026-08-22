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

import sleeperdraft
import wes_http
import wes_nfl
from sleeperdraft import chat as _sd_chat
from sleeperdraft import config as _sd_config
from sleeperdraft import pick as _sd_pick
from sleeperdraft import read as _sd_read
from sleeperdraft import session as _sd_session

# --- the DOM layer lives in `sleeperdraft` -----------------------------------
# Everything that drives the draft room -- the browser session, claiming a seat,
# clicking a pick, the chat, the AUTO-PICK toggle -- moved into a standalone
# package so it can be shared and tested without any of WES around it. What is
# left in this module is the part that is genuinely OURS: Sleeper's scoring
# translated into our stat keys, player identity, and the valued draft board.
#
# These names are re-exported rather than forwarded through wrappers, so
# `wes_sleeper.submit_pick` IS `sleeperdraft.pick.submit_pick` -- one function,
# one place it can be wrong.
BASE = _sd_config.BASE
WEB = _sd_config.WEB
# PINNED TO WHERE OUR PROFILE ALREADY IS. The package defaults to
# ~/.sleeperdraft/profile, which is right for anyone else and wrong for this
# machine -- the logged-in Chrome profile has lived in ~/wes-pc since #039, and
# silently pointing at an empty directory would launch an anonymous browser and
# report a mysteriously empty draft room. Overridable by the environment, same
# as everything else.
PROFILE_DIR = _sd_config.PROFILE_DIR = os.environ.get(
    "WES_SLEEPER_PROFILE_DIR",
    os.path.join(os.path.expanduser("~"), "wes-pc", "sleeper_profile"))
HEADLESS = _sd_config.HEADLESS
BROWSER_CHANNEL = _sd_config.BROWSER_CHANNEL
TOKEN_KEY = _sd_config.TOKEN_KEY
LEAGUE_TTL = _sd_config.LEAGUE_TTL
PLAYERS_TTL = _sd_config.PLAYERS_TTL
_read_token = _sd_config.read_token

# WHO WES IS ON SLEEPER. The package defaults to no account at all, because it
# has no business knowing whose it is; WES has exactly one owner, so the default
# belongs here. The token is re-read against THIS name so the two cannot drift:
# mismatching them is a quiet failure where writes land as somebody else.
USERNAME = os.environ.get("WES_SLEEPER_USER", "awarmwalrus")
_sd_config.USERNAME = USERNAME
TOKEN = _sd_config.TOKEN = _sd_config.read_token(USERNAME)

_Session = _sd_session.Session
_is_login_wall = _sd_session.is_login_wall
authenticate = _sd_session.authenticate
logged_in = _sd_session.logged_in

draft = _sd_read.draft
draft_status_fresh = _sd_read.draft_status_fresh
draft_picks = _sd_read.draft_picks
drafted_player_ids = _sd_read.drafted_player_ids
drafted_player_ids_fresh = _sd_read.drafted_player_ids_fresh
draft_turn = _sd_read.draft_turn
user_id = _sd_read.user_id
slot_in_draft = _sd_read.slot_in_draft
slot_names = _sd_read.slot_names

join_draft = sleeperdraft.join_draft
submit_pick = _sd_pick.submit_pick
autopick_on = _sd_pick.autopick_on
set_autopick = _sd_pick.set_autopick
_norm_name = _sd_pick.norm_name

read_chat = _sd_chat.read_chat
send_chat = _sd_chat.send_chat
parse_chat = _sd_chat.parse_chat

# THE WRITE GATE, wired by NAME rather than by value. The package's default is
# "writes allowed"; WES puts the fantasy kill switch behind it instead. Looked
# up at call time on purpose -- a direct reference would capture the function
# now and ignore anything that replaces it later, and the tests replace it.
_sd_pick.writes_allowed = lambda: LIVE_WRITES_OK()

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


















# The chat lives behind a tab in the draft room and has NO public endpoint —
# every /draft/<id>/chat path 404s — so it is DOM or nothing.
_CHAT_AGO = re.compile(r"^\d+\s+(second|minute|hour|day|week)s?\s+ago$", re.I)




















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
    unfilled = {pos: max(0, n - have.get(pos, 0))
                for pos, n in targets.items()
                if max(0, n - have.get(pos, 0)) > 0}
    # ROSTER COMPLETION IS A CONSTRAINT, NOT A PREFERENCE. Computed here rather
    # than after the shortlist, because it has to narrow what the shortlist is
    # drawn FROM. When every remaining pick is spoken for by a slot still
    # empty, the choice set is those slots -- see wes_draft.must_fill for why
    # no prompt can do this job.
    forced = wes_draft.must_fill(unfilled, rounds - len(mine))
    if forced:
        only = [c for c in board if (c["positions"] or [None])[0] in forced]
        # NEVER leave zero candidates. A draft pick is mandatory; if the pool
        # cannot fill the slot, an incomplete roster beats no pick at all.
        board = only or board

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
        # Named so the model is told it was constrained, rather than quietly
        # handed a board with no kickers on it and left to infer why.
        "must_fill": list(forced),
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






# The player list is a ReactVirtualized grid: ~98,000px of content in a 371px
# viewport, with roughly 59 rows rendered at a time. Anyone outside that window
# is absent from the DOM entirely, and no amount of querySelector will find him.
#
# Programmatic scrollTop does NOT move a virtualised list -- measured, it stays
# put. A real wheel event does, and that is what a human uses anyway.
SCROLL_STEPS = 40
SCROLL_PX = 1500












def LIVE_WRITES_OK():
    """Sleeper writes share the fantasy kill switch rather than inventing a
    second one — one switch to reason about, and #029's `fantasy_live_writes`
    already surfaces at GET /health."""
    import wes_execute
    return wes_execute.writes_enabled()


