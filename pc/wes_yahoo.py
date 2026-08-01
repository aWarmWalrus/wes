"""Yahoo Fantasy client — BROWSER AUTOMATION, read + (later) write (ticket #029).

WHY NOT THE OFFICIAL API (decision 2026-07-17, design §1 + §10): Yahoo now gates
ALL Fantasy API access — read included — behind a manual application + a DocuSign
whose terms FORBID storing/caching their data (delete within 30 days). That is
incompatible with this system's caching-first design and with an autonomous write
bot. So we reach Yahoo the way a person does: a persisted, logged-in browser
session driven by scripted Playwright. This is a personal, single-account
automation (the owner doing by script what they'd do by hand); it may be contrary
to Yahoo's site ToS and is an accepted, owner-approved personal-use risk — kept
single-account, headed, human-paced, low-volume.

STATUS: P0 read WORKING (verified 2026-07-19 against a real league). The
session/profile plumbing, the DOM extractors (roster/scoring/my-teams), and the
normalized-dict contract are all live. Every entry point still degrades to a
plain string, never raising, so importing this file and running the server/tests
is safe even with Playwright absent or the session signed out. In the OFFSEASON,
each player's NBA team + eligible positions come back blank (Yahoo only renders
them in-season); name/slot/status/id are always present.

DESIGN PRINCIPLE (§10, mirrors §8.1-8.2): this is a DETERMINISTIC script, not an
LLM driving the page. Callers' optimizer decides the *move*; the script replays
it. The LLM never free-drives the browser. That is what keeps the §5 rails
(validate → guardrail → confirmation token) ahead of every write click.

THE CONTRACT (stable; valuation/optimizer/formatters depend on it):
  a player  -> {name, team, positions:[..], slot, status, player_key}
  scoring   -> {scoring_type, categories:[..]}
Whatever the extraction source, it emits these shapes. `format_roster` /
`format_scoring` render them and are reused verbatim from the API-era code.

League content (team names, chat) is EXTERNAL, UNTRUSTED text: callers hand it to
the model as quoted data, never instructions — same rule as wes_nba.py.
"""
import os
import re

# --- config -----------------------------------------------------------------
# The logged-in browser profile (cookies/session) is persisted here on the PC.
# NEVER the repo, NEVER chat — it is the equivalent of a live credential.
PROFILE_DIR = os.environ.get(
    "WES_YAHOO_PROFILE_DIR",
    os.path.join(os.path.expanduser("~"), "wes-pc", "yahoo_profile"))
# Headed by default: login/2FA needs a visible window, and a headed browser is
# far less likely to trip bot-detection than headless. Scheduled unattended runs
# may flip this via env once a session is established and proven.
HEADLESS = os.environ.get("WES_YAHOO_HEADLESS", "0") == "1"
# Drive the REAL installed Chrome, not Playwright's bundled Chromium. Google SSO
# ("this browser may not be secure") blocks Chromium + the automation flags; real
# Chrome with those flags stripped (see _Session) reads as an ordinary browser.
# Falls back to bundled Chromium if Chrome isn't installed. Set to "" to force
# bundled Chromium; "msedge" also works.
BROWSER_CHANNEL = os.environ.get("WES_YAHOO_BROWSER_CHANNEL", "chrome")
NAV_TIMEOUT_MS = int(os.environ.get("WES_YAHOO_NAV_TIMEOUT_MS", "20000"))
LOGIN_URL = "https://login.yahoo.com"

# --- per-sport sites (#029 P7) -----------------------------------------------
# Yahoo puts each sport on its own host AND uses a DIFFERENT url path segment per
# sport. That asymmetry is the trap: football is "/f1/", NOT "/nfl/", so anyone
# extending this by analogy from NBA gets 404s. Verified live 2026-07-29 by
# pc/yahoo_league_discover.py.
#
#   NBA: basketball.fantasysports.yahoo.com/nba/<league>/<team>
#   NFL: football.fantasysports.yahoo.com/f1/<league>/<team>
#
# The dotted KEY prefix is Yahoo's game code either way ("nba.l.114020.t.1",
# "nfl.l.957011.t.4"), which is what teams.yaml records — so every entry point
# below can derive its sport from the key it was already given, and no caller
# signature had to change.
_SITES = {
    "nba": {"home": "https://basketball.fantasysports.yahoo.com",
            "path": "nba", "label": "Basketball"},
    "nfl": {"home": "https://football.fantasysports.yahoo.com",
            "path": "f1", "label": "Football"},
}
DEFAULT_SPORT = "nba"
# Kept as a module-level name because callers and docs referred to it; it is now
# just the default sport's home. Prefer _home(sport) / _team_url() in new code.
FANTASY_HOME = _SITES[DEFAULT_SPORT]["home"]


def _site(sport=None):
    """The site table for `sport`. An unknown/absent sport degrades to NBA so a
    typo in teams.yaml behaves like the pre-multi-sport code instead of raising."""
    return _SITES.get(str(sport or DEFAULT_SPORT).lower(), _SITES[DEFAULT_SPORT])


def sports():
    """The sports this module can reach, for callers that enumerate (my_teams)."""
    return list(_SITES)


def _sport_of(key):
    """Sport from a Yahoo dotted key ('nfl.l.957011.t.4' -> 'nfl').

    Historic NBA keys used the numeric game id instead of a name ("466.l.12345"),
    and teams.yaml still allows that, so an unrecognized prefix means NBA."""
    head = str(key or "").split(".", 1)[0].lower()
    return head if head in _SITES else DEFAULT_SPORT


def _home(sport=None):
    return _site(sport)["home"]


def _league_url(key, suffix=""):
    """Full URL for a league page, sport derived from the key."""
    s = _site(_sport_of(key))
    tail = f"/{suffix.lstrip('/')}" if suffix else ""
    return f"{s['home']}/{s['path']}/{_league_of(key)}{tail}"


def _team_url(key, suffix=""):
    """Full URL for a team page, sport derived from the key."""
    s = _site(_sport_of(key))
    tail = f"/{suffix.lstrip('/')}" if suffix else ""
    return f"{s['home']}/{s['path']}/{_league_of(key)}/{_team_of(key)}{tail}"

_UNAVAILABLE = "I couldn't reach Yahoo Fantasy just now."
_NOT_CONFIGURED = (
    "Yahoo Fantasy isn't connected yet — it needs a one-time browser sign-in "
    "(run `python pc\\yahoo_connect.py` on the PC).")
_NO_PLAYWRIGHT = (
    "Yahoo Fantasy needs the browser-automation dependency, which isn't "
    "installed yet (`pip install playwright` then `playwright install chromium`).")
_NOT_IMPLEMENTED = (
    "Yahoo Fantasy reading isn't wired up yet — the page scrapers still need to "
    "be written against the real logged-in UI (ticket #029 P0).")


# --- session plumbing -------------------------------------------------------
def _have_playwright():
    try:
        import playwright.sync_api  # noqa: F401
        return True
    except ImportError:
        return False


def has_session():
    """True once the owner has signed in and the profile has been persisted.

    A populated profile dir is the proxy for 'a session exists' — we don't and
    can't validate the cookies without launching, so callers still degrade
    gracefully if the session has since expired."""
    return os.path.isdir(PROFILE_DIR) and bool(os.listdir(PROFILE_DIR))


def configured():
    """Cheap check: browser dep present AND a session profile exists on disk.
    Does NOT prove the session is still valid — use logged_in() for that."""
    return _have_playwright() and has_session()


# Yahoo's login cookie set. Presence of these in the profile == a live session.
# (The DOM is NOT a reliable signal — signed-in fantasy pages still carry a
# login.yahoo.com link in hidden menus, which false-negatives.)
_AUTH_COOKIES = {"A1", "T", "Y"}


def logged_in():
    """Deeper than configured(): is the persisted session actually signed in?
    Checks the profile's Yahoo auth cookies (the reliable signal) rather than
    scraping the masthead. Launches the context (not free); degrades to False,
    never raises."""
    if not configured():
        return False
    try:
        with _Session() as page:
            names = {c.get("name") for c in page.context.cookies()
                     if "yahoo" in (c.get("domain") or "")}
            return bool(_AUTH_COOKIES & names)
    except Exception as e:  # noqa: BLE001
        print(f"[yahoo] logged_in check failed: {e!r}", flush=True)
        return False


class _Session:
    """A launched persistent browser context. Use as a context manager so the
    browser is always closed even on error:

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
        # Strip the automation tells Google SSO blocks on: the webdriver flag
        # (--disable-blink-features=AutomationControlled) and the enable-automation
        # switch/infobar. A persistent context reuses PROFILE_DIR so a one-time
        # login sticks.
        launch = dict(
            headless=self.headless,
            args=["--disable-blink-features=AutomationControlled"],
            ignore_default_args=["--enable-automation"],
        )
        try:
            self._ctx = self._pw.chromium.launch_persistent_context(
                PROFILE_DIR, channel=BROWSER_CHANNEL or None, **launch)
        except Exception as e:  # noqa: BLE001
            # Chrome/Edge not installed -> bundled Chromium (may hit the Google
            # block; see BROWSER_CHANNEL note).
            print(f"[yahoo] channel {BROWSER_CHANNEL!r} unavailable ({e!r}); "
                  "falling back to bundled Chromium.", flush=True)
            self._ctx = self._pw.chromium.launch_persistent_context(
                PROFILE_DIR, **launch)
        page = self._ctx.pages[0] if self._ctx.pages else self._ctx.new_page()
        page.set_default_timeout(NAV_TIMEOUT_MS)
        return page

    def __exit__(self, *exc):
        try:
            if self._ctx:
                self._ctx.close()
        finally:
            if self._pw:
                self._pw.stop()
        return False


def login():
    """One-time interactive sign-in. Opens a HEADED browser at Yahoo login; the
    owner signs in (including 2FA) and the persistent profile keeps the session.
    Blocks until the owner confirms on the console. Returns True on success.

    Called by pc/yahoo_connect.py — not from a turn (it needs a human)."""
    if not _have_playwright():
        print(_NO_PLAYWRIGHT)
        return False
    with _Session(headless=False) as page:
        page.goto(LOGIN_URL)
        print("A browser window is open. Sign in to Yahoo (and finish 2FA).")
        print("When you can see your account is logged in, come back here.")
        try:
            input("Press Enter once you're signed in... ")
        except (EOFError, KeyboardInterrupt):
            print("\naborted — session not saved.")
            return False
        # Prove the session by loading the fantasy home while still logged in.
        try:
            page.goto(FANTASY_HOME, wait_until="domcontentloaded")
        except Exception as e:  # noqa: BLE001
            print(f"couldn't confirm the fantasy page loaded: {e!r}")
            return False
    print(f"Session saved to {PROFILE_DIR}")
    return True


def _scrape(url, extract):
    """Launch the session, navigate, run `extract(page)`. Any failure — no
    session, expired login, Playwright missing, selector miss — degrades to a
    string; a Yahoo problem must never raise into a turn."""
    if not _have_playwright():
        return _NO_PLAYWRIGHT
    if not has_session():
        return _NOT_CONFIGURED
    try:
        with _Session() as page:
            # Yahoo fantasy pages are heavy (ads/trackers); waiting for the full
            # "load" event routinely exceeds the budget. DOM-ready is enough —
            # the extractors wait on their own specific selectors.
            page.goto(url, wait_until="domcontentloaded")
            return extract(page)
    except NotImplementedError:
        return _NOT_IMPLEMENTED
    except Exception as e:  # noqa: BLE001
        print(f"[yahoo] scrape failed for {url}: {e!r}", flush=True)
        return _UNAVAILABLE


# --- DOM extractors (STUBS — selectors need the real logged-in UI) ----------
# These are the ONLY pieces that must be written against Yahoo's live pages.
# Each must return the normalized contract (see module docstring) so everything
# downstream — format_*, valuation, the optimizer — is untouched when they land.
# A parse canary (tests, WES_YAHOO_LIVE=1) will pin them once they exist and
# catch selector drift when Yahoo reskins.
# Verified against the live roster page 2026-07-19 (league 114020). The roster
# table is `table.ysf-rosterswapper`; each player row carries the slot in
# `span.pos-label[data-pos]`, the player in `a.name.F-link` (+ Yahoo player id in
# the /players/<id> href), and injury status in `span.player-status`. Team abbr +
# eligible positions live in `.ysf-player-detail`, which is EMPTY in the offseason
# (no games) — parsed best-effort, populated in-season (canary verifies).
# The "<team> - <positions>" cell lives under a DIFFERENT selector per sport, and
# the NBA one is actively misleading on NFL (verified live 2026-07-29 on
# nfl.l.957011): `.ysf-player-detail` holds "Bkn - PG,SG" on basketball but the
# GAME time ("Sun 1:25 pm vs Was") on football, where the team/position pair sits
# in `span.Fz-xxs` ("Phi - QB"). So try the candidates in order and accept the
# first whose text actually has the team/position SHAPE, rather than trusting any
# one selector. Silent failure here is expensive: with no positions, no player is
# eligible for any slot and the optimizer benches the entire roster.
_DETAIL_SELECTORS = (".ysf-player-detail", "span.Fz-xxs", ".Fz-xxs")
# A position token is short and alphanumeric ("PG", "W/R/T", "DEF"); this rejects
# a game string that happens to contain " - ".
_POS_RE = re.compile(r"^[A-Za-z0-9/+]{1,6}$")


def _detail_team_positions(row):
    """Best-effort (team_abbr, [positions]) for a roster row, either sport.
    Returns ("", []) when the page doesn't expose it (NBA offseason blanks it)."""
    for sel in _DETAIL_SELECTORS:
        el = row.query_selector(sel)
        text = " ".join((el.inner_text() or "").split()) if el else ""
        if " - " not in text:
            continue
        team, _, pos = text.partition(" - ")
        positions = [p.strip() for p in re.split(r"[,/]", pos) if p.strip()]
        # "/" is a separator inside NBA multi-position ("PG/SG") but also part of
        # NFL slot names; splitting on it is right for POSITIONS, which are
        # always atomic ("WR", not "W/R/T").
        if positions and all(_POS_RE.match(p) for p in positions):
            return team.strip(), positions
    return "", []


def _detail_game(row):
    """The row's game blurb ('Sun 1:25 pm vs Was'), or "" — the raw material for
    an NFL "is this player on a bye?" check. Captured, not interpreted: turning
    it into `playing` belongs with the weekly-cadence work (#029)."""
    el = row.query_selector(".ysf-player-detail")
    text = " ".join((el.inner_text() or "").split()) if el else ""
    return "" if " - " in text else text


# Yahoo's status span carries the injury abbreviation ("O", "Q", "IR") — but on
# football it also picks up player-note chrome ("Video ForecastNo new player
# Notes"), which would render as a fake injury flag on every healthy player.
# Drop text that is obviously that chrome; keep anything else VERBATIM rather
# than whitelisting, because an unrecognized status is safe (it just isn't in
# _OUT_STATUS, so the player stays startable) while dropping a real one is not.
_STATUS_JUNK = ("note", "forecast", "video")


def _clean_status(text):
    t = " ".join((text or "").split())
    low = t.lower()
    if not t or len(t) > 16 or any(j in low for j in _STATUS_JUNK):
        return ""
    return t


def _extract_roster(page):
    """DOM -> list[player-dict] in the normalized contract."""
    out = []
    for row in page.query_selector_all("table.ysf-rosterswapper tbody tr"):
        name_el = row.query_selector("a.name")
        if not name_el:
            continue  # header / empty-slot / spacer rows carry no player anchor
        name = (name_el.get_attribute("title") or name_el.inner_text() or "").strip()
        if not name:
            continue
        m = re.search(r"/players/(\d+)", name_el.get_attribute("href") or "")
        slot_el = row.query_selector("span.pos-label")
        slot = ((slot_el.get_attribute("data-pos") or slot_el.inner_text())
                if slot_el else "").strip()
        status_el = row.query_selector("span.player-status")
        status = _clean_status(status_el.inner_text() if status_el else "")
        team, positions = _detail_team_positions(row)
        out.append({
            "name": name,
            "team": team,
            "positions": positions,
            "slot": slot,
            "status": status,
            "game": _detail_game(row),
            "player_key": m.group(1) if m else "",
        })
    return out


def _extract_scoring(page):
    """DOM -> {scoring_type, categories}. The settings page renders these as a
    label/value list; read them out of the body text (robust to layout churn)."""
    body = page.inner_text("body")
    m = re.search(r"Scoring Type:\s*([^\n\t]+)", body)
    raw = (m.group(1).strip().lower() if m else "")
    stype = {"rotisserie": "roto", "head-to-head": "head",
             "points": "point"}.get(raw, raw)
    cats = []
    m = re.search(r"Stat Categories:\s*([^\n]+)", body)
    if m:
        # abbreviations are the parenthesised tokens, e.g. "... (PTS), ... (REB)"
        cats = re.findall(r"\(([A-Z0-9%]+)\)", m.group(1))
    return {"scoring_type": stype, "categories": cats}


def _extract_settings_lines(page):
    """The settings page's tables as flat text lines.

    Deliberately semantics-FREE: this module stays a scraper, and the meaning of
    'Passing Yards 25 yards per point' belongs to the sport that has passing
    yards (wes_nfl.parse_scoring / parse_roster_slots). Scoped to <table> so the
    surrounding news/nav text doesn't come along."""
    lines = []
    for table in page.query_selector_all("table"):
        for raw in (table.inner_text() or "").split("\n"):
            text = " ".join(raw.split())
            if text:
                lines.append(text)
    return lines


def _my_team_key(page, sport):
    """The signed-in owner's team key for the league page currently loaded, read
    from Yahoo's own "My Team" nav link.

    WHY NOT THE DASHBOARD LINKS: the league page links the current week's
    OPPONENT as well as your own team, so link-scraping alone reports two teams
    for one league (observed 2026-07-29: nfl.l.957011 showed both t.4 and the
    opponent t.8). Text markers are no better — "My Team" / "Edit Team" appear in
    the nav on *every* team's page. Only the nav link's href identifies you.
    Getting this wrong means managing a stranger's roster, so it is worth the
    extra navigation."""
    s = _site(sport)
    for a in page.query_selector_all("a"):
        label = " ".join((a.inner_text() or "").split()).lower()
        if label != "my team":
            continue
        href = (a.get_attribute("href") or "").split("?")[0]
        parts = [p for p in href.split("fantasysports.yahoo.com")[-1].split("/") if p]
        if len(parts) >= 3 and parts[0] == s["path"] \
                and parts[1].isdigit() and parts[2].isdigit():
            return f"{sport}.l.{parts[1]}.t.{parts[2]}"
    return ""


def _extract_league_ids(page, sport):
    """DOM -> the league ids linked from a dashboard, for one sport."""
    s = _site(sport)
    found = []
    for a in page.query_selector_all(f"a[href*='/{s['path']}/']"):
        tail = (a.get_attribute("href") or "").split(
            "fantasysports.yahoo.com")[-1].split("?")[0]
        parts = [p for p in tail.split("/") if p]
        if len(parts) >= 2 and parts[0] == s["path"] and parts[1].isdigit() \
                and parts[1] not in found:
            found.append(parts[1])
    return found


def _extract_my_teams(page, sport=DEFAULT_SPORT):
    """DOM -> list[(team_name, team_key)] for ONE sport's dashboard. Names come
    from the link text where present; callers enrich blanks from the page title.

    NOTE this may include a team that is NOT yours (see _my_team_key) — it is the
    candidate set, not the answer. my_teams() resolves ownership per league."""
    s = _site(sport)
    names = {}  # key -> best (non-empty) name seen; a team has several links,
    #             some with empty text (nav/logo) — keep whichever names it.
    for a in page.query_selector_all(f"a[href*='/{s['path']}/']"):
        tail = (a.get_attribute("href") or "").split(
            "fantasysports.yahoo.com")[-1].split("?")[0]
        parts = [p for p in tail.split("/") if p]
        if len(parts) >= 3 and parts[0] == s["path"] and parts[1].isdigit() \
                and parts[2].isdigit():
            key = f"{sport}.l.{parts[1]}.t.{parts[2]}"
            name = (a.get_attribute("title") or a.inner_text() or "").strip()
            if name or key not in names:
                names.setdefault(key, "")
                if name:
                    names[key] = name
    return [(name or key, key) for key, name in names.items()]


# --- formatters the model reads (REUSED verbatim; source-agnostic) ----------
# Design rule (design §8.3): the model never sees a raw page. One line per player
# keeps a 13-man roster at ~400 tokens. These consume the normalized contract,
# so they didn't change when the source flipped from API JSON to the DOM.
def format_roster(players, team_name=""):
    if not players:
        return "That roster came back empty."
    lines = [f"Roster{f' — {team_name}' if team_name else ''} ({len(players)}):"]
    for p in players:
        bits = [p["name"]]
        if p.get("team"):
            bits.append(p["team"])
        if p.get("positions"):
            bits.append("/".join(p["positions"]))
        if p.get("slot"):
            bits.append(f"slot {p['slot']}")
        if p.get("status"):
            bits.append(f"** {p['status']} **")
        lines.append("  " + " · ".join(bits))
    return "\n".join(lines)


def format_scoring(scoring):
    kind = {"head": "head-to-head", "headone": "H2H points",
            "roto": "rotisserie", "point": "points"}.get(
                scoring.get("scoring_type", ""), scoring.get("scoring_type", "?"))
    cats = ", ".join(scoring.get("categories") or [])
    if cats:
        return f"Scoring: {kind}. Categories that count: {cats}."
    # No categories means two DIFFERENT things and conflating them hides a bug:
    # a rotisserie league always has categories, so their absence is a scrape
    # failure worth saying "unknown" about; a POINTS league has none by design,
    # and calling that unknown reads as breakage when nothing broke.
    if scoring.get("scoring_type") == "roto":
        return f"Scoring: {kind}, but the categories came back unknown."
    return (f"Scoring: {kind}, points-based (per-stat weights rather than "
            f"categories).")


# --- entry points (degrade to a string, never raise) ------------------------
def my_teams(sport=None):
    """The owner's fantasy teams. `sport` limits to one ('nba'/'nfl'); None scans
    every sport in _SITES — the default, because scanning only NBA is exactly how
    two real NFL leagues stayed invisible until 2026-07-29.

    Ownership is resolved per league from the "My Team" nav link rather than from
    dashboard link text, so an opponent's team is never reported as yours."""
    if not _have_playwright():
        return _NO_PLAYWRIGHT
    if not has_session():
        return _NOT_CONFIGURED
    wanted = [sport.lower()] if sport else sports()
    found = []          # (sport, name, key)
    try:
        with _Session() as page:
            for sp in wanted:
                if sp not in _SITES:
                    continue
                try:
                    page.goto(_home(sp), wait_until="domcontentloaded")
                except Exception as e:  # noqa: BLE001
                    print(f"[yahoo] {sp} dashboard failed: {e!r}", flush=True)
                    continue
                candidates = dict((k, n) for n, k in _extract_my_teams(page, sp))
                # Every league the dashboard mentions, including ones whose link
                # carries no team id (pre-draft leagues do this).
                for league in _extract_league_ids(page, sp):
                    league_key = f"{sp}.l.{league}"
                    try:
                        page.goto(_league_url(league_key),
                                  wait_until="domcontentloaded")
                    except Exception:  # noqa: BLE001
                        continue
                    key = _my_team_key(page, sp)
                    if not key:
                        continue
                    name = candidates.get(key, "")
                    if not name:
                        # Title reads "<league> - <team> | Fantasy Football | ...".
                        try:
                            page.goto(_team_url(key), wait_until="domcontentloaded")
                            title = page.title().split(" | ")[0]
                            if " - " in title:
                                name = title.split(" - ")[-1].strip()
                        except Exception:  # noqa: BLE001
                            pass
                    found.append((sp, name or key, key))
    except Exception as e:  # noqa: BLE001
        print(f"[yahoo] my_teams failed: {e!r}", flush=True)
        return _UNAVAILABLE
    if not found:
        scanned = "/".join(_site(s)["label"] for s in wanted)
        return f"You don't seem to have any {scanned} fantasy teams right now."
    lines = [f"{_site(sp)['label']}: {n} ({k})" if n != k
             else f"{_site(sp)['label']}: {k}" for sp, n, k in found]
    return "Your fantasy teams:\n  " + "\n  ".join(lines)


def roster(team_key):
    """A team's current roster as a compact table. Sport comes from the key."""
    result = _scrape(_team_url(team_key), _extract_roster)
    if not isinstance(result, list):
        return result
    return format_roster(result)


def roster_players(team_key):
    """A team's roster as RAW normalized player dicts (list), or a degradation
    string on failure — for the optimizer/valuation engine, which needs the
    structured players, not the formatted string `roster()` returns."""
    return _scrape(_team_url(team_key), _extract_roster)


def league_scoring(league_key):
    """The league's scoring format — what 'value' means in THIS league."""
    result = _scrape(_league_url(league_key, "settings"), _extract_scoring)
    if not isinstance(result, dict):
        return result
    return format_scoring(result)


# --- free agents (#035) ------------------------------------------------------
# The league's player list defaults to AVAILABLE players ranked best-first,
# which is exactly the shortlist a pickup decision needs. Verified 2026-07-31:
# 25 rows, all roster-status "FA"; `count=`/`status=` params did NOT change the
# page size, so this reads the default view rather than pretending to control it.
#
# Rows reuse the SAME shapes as the roster table (`a.name`, `span.Fz-xxs` for
# "Jax - WR"), so the trap documented on `_detail_team_positions` applies here
# too and the same helper is reused rather than re-derived.
_FA_STATUS_COL = 3     # "Roster Status": "FA", or a waiver marker in-season


def _extract_free_agents(page):
    """DOM -> [{name, team, positions, roster_status, is_free_agent, bye,
    player_key}]. Anything whose status is not exactly "FA" is treated as
    NOT a free pickup — in-season Yahoo shows waiver markers here, and
    guessing that an unknown marker means "just add them" would turn a waiver
    claim into a failed action at best."""
    out = []
    for row in page.query_selector_all("#players-table tbody tr"):
        name_el = row.query_selector("a.name")
        if not name_el:
            continue
        name = (name_el.get_attribute("title") or name_el.inner_text() or "").strip()
        if not name:
            continue
        m = re.search(r"/players/(\d+)", name_el.get_attribute("href") or "")
        team, positions = _detail_team_positions(row)
        cells = [" ".join((td.inner_text() or "").split())
                 for td in row.query_selector_all("td")]
        status = cells[_FA_STATUS_COL] if len(cells) > _FA_STATUS_COL else ""
        out.append({
            "name": name,
            "team": team,
            "positions": positions,
            "roster_status": status,
            "is_free_agent": status.upper() == "FA",
            "player_key": m.group(1) if m else "",
        })
    return out


def free_agents(league_key):
    """The league's available players (Yahoo's default best-first ranking), or
    a degradation string. These are CANDIDATES only — valuing them is the
    engine's job, and deliberately uses the same ESPN pool that values rostered
    players, so a pickup is compared against a drop on one consistent scale
    rather than against Yahoo's own displayed projection."""
    return _scrape(_league_url(league_key, "players"), _extract_free_agents)


def league_settings_lines(league_key):
    """The league settings page as flat text lines (list), or a degradation
    string. Raw material for the sport-specific parsers — NFL points weights and
    the authoritative roster-slot list both live in this text."""
    return _scrape(_league_url(league_key, "settings"), _extract_settings_lines)


def league_categories(league_key):
    """The league's raw scoring-category list (e.g. ['PTS','REB',...]) for the
    valuation engine, or [] on any failure. Unlike league_scoring (a formatted
    string for the model), this returns the structured list."""
    result = _scrape(_league_url(league_key, "settings"), _extract_scoring)
    if not isinstance(result, dict):
        return []
    return result.get("categories") or []


# --- team registry (identities + policy; NO secrets) ------------------------
# teams.yaml names the owner's leagues/teams and their autonomy policy (design
# §4). It holds NO secrets — the live session cookies live in the browser
# profile (PROFILE_DIR), never here. Copy fantasy/teams.example.yaml to a
# PC-local path and point WES_FANTASY_TEAMS at it.
TEAMS_FILE = os.environ.get(
    "WES_FANTASY_TEAMS",
    os.path.join(os.path.expanduser("~"), "wes-pc", "teams.yaml"))
_teams_cache = None


def _load_teams(force=False):
    """Parsed teams.yaml as a dict, cached. Returns {} if unreadable (no PyYAML,
    missing file, parse error) so the tool degrades to a hint, never raises."""
    global _teams_cache
    if _teams_cache is not None and not force:
        return _teams_cache
    try:
        import yaml
    except ImportError:
        return {}
    try:
        with open(TEAMS_FILE, encoding="utf-8") as f:
            _teams_cache = yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError):
        _teams_cache = {}
    return _teams_cache


def configured_teams(include_inactive=False):
    """The team records from teams.yaml ([] if none / unreadable).

    Records with `active: false` are SKIPPED by default. That flag exists because
    Yahoo league keys are per-SEASON (#033): when a season rolls over, the old
    key stops resolving and every scrape against it returns empty. Deleting the
    entry loses the history; leaving it selectable is worse — it was first in the
    file, so it became the DEFAULT team and silently broke every fantasy question
    ("who should I start" resolved to a league that no longer exists). Mark it
    inactive instead. Pass include_inactive=True to see the full file."""
    teams = _load_teams().get("teams")
    if not isinstance(teams, list):
        return []
    if include_inactive:
        return teams
    return [t for t in teams
            if isinstance(t, dict) and t.get("active", True) is not False]


def _resolve_team(name=None):
    """Pick a configured team by (case-insensitive, substring) name, or the
    first one when name is omitted. Returns (team_dict, error_string) with
    exactly one side non-None — or (None, None) when nothing is configured, so
    the caller can fall back to a live my_teams() listing."""
    teams = configured_teams()
    if not teams:
        return None, None
    if not name:
        return teams[0], None
    lo = name.strip().lower()
    for t in teams:
        if lo in str(t.get("name", "")).lower():
            return t, None
    have = ", ".join(str(t.get("name", "?")) for t in teams)
    return None, f"You don't have a fantasy team called '{name}'. You have: {have}."


def fantasy_my_team(team=None):
    """The owner's fantasy team, read LIVE: the current roster plus the league's
    scoring settings, for the configured team (or the one named). This is the P0
    read tool (ticket #029). Degrades to a string on any problem, never raises.

    When nothing is configured yet, falls back to listing the Yahoo teams found
    on the signed-in account, with a hint to set up teams.yaml — so the tool is
    useful before the config exists."""
    chosen, err = _resolve_team(team)
    if err:
        return err
    if chosen is None:
        listing = my_teams()  # a real listing, or a degradation hint
        if listing.startswith(("Your NBA", "You don't seem")):
            return (listing + "\n(Set up fantasy/teams.yaml + WES_FANTASY_TEAMS "
                    "to pick a default team.)")
        return listing
    name = str(chosen.get("name", "")).strip()
    team_key = chosen.get("team_key", "")
    league_key = chosen.get("league_key", "") or team_key
    roster_str = roster(team_key)
    if not roster_str.startswith("Roster"):
        return roster_str  # degradation hint — surface it plainly
    out = f"{name}\n{roster_str}" if name else roster_str
    scoring_str = league_scoring(league_key)
    if scoring_str.startswith("Scoring:"):  # drop a scoring miss; roster is the answer
        out += "\n" + scoring_str
    return out


# --- key helpers ------------------------------------------------------------
# Yahoo keys look like "466.l.12345.t.3" (game.l.league.t.team). The web URLs
# use the bare league / team numbers, so pull them out.
def _league_of(key):
    parts = key.split(".l.")
    return parts[1].split(".")[0] if len(parts) > 1 else key


def _team_of(key):
    parts = key.split(".t.")
    return parts[1].split(".")[0] if len(parts) > 1 else ""
