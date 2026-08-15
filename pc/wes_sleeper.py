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
# Visible by default, like the Yahoo session: the one-time login is interactive,
# and a headless window the owner cannot see is a bad place to discover that a
# session expired.
HEADLESS = os.environ.get("WES_SLEEPER_HEADLESS", "0") == "1"
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
TOKEN = os.environ.get("WES_SLEEPER_TOKEN", "")

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
        # Team defences have no person-name; Sleeper keys them by team abbr.
        if pos == "DEF" and not name:
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
            "status": p.get("injury_status") or "",
        }
    return out


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

    def __init__(self, headless=None):
        self.headless = HEADLESS if headless is None else headless
        self._pw = None
        self._ctx = None

    def __enter__(self):
        from playwright.sync_api import sync_playwright
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
        return page

    def __exit__(self, *exc):
        try:
            if self._ctx:
                self._ctx.close()
        finally:
            if self._pw:
                self._pw.stop()
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


def draft_picks(draft_id, _get_fn=None):
    """Every pick made so far, oldest first. Short TTL: during a live draft this
    is the fast-moving fact everything else depends on."""
    return _get(f"/draft/{draft_id}/picks", ttl=15.0, _get_fn=_get_fn)


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
    if proj:
        return proj
    pool, _failed = wes_nfl.pool_by_position()
    return pool


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
    scoring = league_scoring(league_id, _get_fn)
    slots = league_slots(league_id, _get_fn)
    targets, flex, flex_pos = wes_draft.targets_from_slots(slots)

    have = {}
    my_players = []
    for pid in mine:
        info = index.get(pid) or {}
        my_players.append({"team": info.get("team"),
                           "positions": info.get("positions") or []})
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
    by_espn = {str(p["espn_id"]): p for p in pool if p.get("espn_id")}
    board = []
    for pid, info in index.items():
        if pid in taken:
            continue
        espn = info.get("espn_id")
        stat = by_espn.get(espn) if espn else None
        if stat is None:
            continue                      # unvalued: never recommend a guess
        pos = (info.get("positions") or [None])[0]
        val = wes_nfl.fantasy_points(wes_nfl.per_game(stat).get("cats"),
                                     scoring["weights"], scoring["tiers"])
        board.append({"name": info["name"], "positions": info.get("positions"),
                      "team": info.get("team"), "player_key": pid,
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
        p["need_bump"] = 2.0 * gap if gap else (
            0.5 if (pos in flex_pos and flex) else 0.0)
        p["fit_penalty"] = penalty
        p["fit_reasons"] = why
        p["adj_value"] = round(p["vor"] + p["need_bump"] - penalty, 2)
        fitted.append(p)
    board = fitted or board
    board.sort(key=lambda x: x["adj_value"], reverse=True)

    return {
        "round": made // teams + 1, "picks_made": made,
        "picks_until_turn": wait, "on_the_clock": wait == 0,
        "my_slot": my_slot, "teams": teams, "roster_size": len(mine),
        "candidates": board[:limit],
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


def submit_pick(draft_id, player_key, player_name, _session_cls=None,
                _picks_fn=None, _sleep_fn=None):
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
    with (_session_cls or _Session)() as page:
        if not authenticate(page):
            raise RuntimeError("no WES_SLEEPER_TOKEN — cannot reach the draft")
        page.goto(f"{WEB}/draft/nfl/{draft_id}",
                  wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(6000)

        row = None
        for r in page.query_selector_all(".player-rank-item2"):
            nm = r.query_selector(".name-wrapper")
            if nm and _norm_name((nm.inner_text() or "").split("\n")[0]) == want:
                row = r
                break
        if row is None:
            raise RuntimeError(
                f"{player_name!r} is not in the draft room's available list — "
                f"refusing to click a different row")

        btn = row.query_selector(".draft-button")
        if btn is None:
            raise RuntimeError(f"no draft control on {player_name}'s row")
        if "disable" in (btn.get_attribute("class") or ""):
            raise RuntimeError(
                "the draft button is disabled — not on the clock, so a click "
                "would do nothing and we would report a pick that never was")
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
        got = {str(p.get("player_id")) for p in picks if p.get("player_id")}
        if str(player_key) in got:
            return True
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
