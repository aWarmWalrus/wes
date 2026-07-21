---
id: 029
title: Fantasy GM — autonomous fantasy-team management (epic)
status: open
priority: high
created: 2026-07-16
closed:
tags: [nba, fantasy, yahoo, oauth, tools, agentic, actions, scheduling, rails, epic]
related: [docs/fantasy-gm-design.md, "#027", "#028", "#026", "#005", "#012", "#004", "#002"]
---

## Problem / Goal
Give Jarvis the ability to **run a fantasy team like a GM** — read the league,
value players against *your league's* scoring, decide the moves (lineup, waivers,
trades), and, gated by a per-team autonomy setting, **execute them** on the
platform, reporting back over Discord/voice.

This is an **epic / umbrella** ticket. The full design + phased roadmap lives in
**`docs/fantasy-gm-design.md`** (read it for the substance); this ticket tracks
status and links out to the per-phase tickets as they're spun up (#030+).

## Decisions (2026-07-16, from owner)
- **Sport:** NBA first (reuses `wes_nba.py` + r/GoNets; engine kept sport-agnostic).
- **Platform:** **Yahoo** (owner's league), reached via **browser automation**,
  NOT the official API. *(Revised 2026-07-17 — the API was ruled out; see Notes.
  The original rationale "only platform with official read+write OAuth" no longer
  applies: the executor is now UI-based, so a write API is no longer required.)*
- **Autonomy:** **per-team, configurable.** Jarvis runs a *portfolio* of teams;
  each is independently `advise` / `propose` / `auto` (and per action type). New
  teams default to `propose`; `auto` is opt-in after a shadow-mode soak.

## Approach
Ingestion → valuation → decision → gated execution, wrapped in per-team config,
scheduling, and rails. New pieces: `pc/wes_yahoo.py` (OAuth client), the
valuation/lineup-optimizer engine, a **single gated executor + action ledger**,
and `nba/fantasy/teams.yaml` (secrets in PC env, never the repo). Reuses the
planner (#028), deep-model routing (#026), scheduler (#005), durable memory
(#012), and the smart-home action/rails pattern (#004). Full rails in the design
doc §5 (gated executor, never-drop/FAAB/volume guardrails, shadow mode,
confirmation tokens, idempotency, injection guard).

## Phases (each becomes its own ticket when started; full detail in the doc §7)
- [x] **P0** — Yahoo read + league sync — **DONE 2026-07-20**. `fantasy_my_team`
      wired into the live server (12th tool); real roster + scoring answered
      end-to-end from the owner's league ("Enemies of Whiffer", nba.l.114020);
      PC-local `teams.yaml` configured; eval-gated. See Notes.
- [x] **P1** — Valuation + full stat lines — **DONE 2026-07-20**. `fantasy_player_value`
      values a player (optionally vs another) in the league's roto categories from
      real ESPN season stats; new `pc/wes_fantasy.py` engine. See Notes.
- [ ] **P2** — Daily lineup optimizer (advise / dry-run, explained, shadow-soaked)
- [ ] **P3** — Executor + autonomy config + rails (first real Yahoo writes)
- [ ] **P4** — Scheduling + pre-lock monitoring / late swaps
- [ ] **P5** — Waiver / FAAB engine
- [ ] **P6** — Trades + matchup (punt) strategy
- [ ] **P7 (stretch)** — Multi-team portfolio + NFL/MLB generalization

Critical path to the headline feature (Jarvis auto-sets my lineup): P0→P1→P2→P3→P4.

**#028 is NOT a blocker** (decided 2026-07-16, design §8.1): the daily run is a
fixed DAG of deterministic code with bounded LLM calls at judgment nodes, not an
agentic loop. The optimizer is exact code (ILP/Hungarian); the model only explains
and handles edge cases. #028's planner serves the *ad-hoc* channel instead.

## Acceptance (epic-level — sub-tickets carry the detailed criteria)
- [x] Jarvis reads a real Yahoo NBA league (roster + scoring) via a tool
      (`fantasy_my_team`, 2026-07-20). *Matchup/standings scrapers not yet
      written — folded into a later phase; not needed for the P0 gate.*
- [x] Player value is computed against *that league's* scoring, no hallucinated
      stats (`fantasy_player_value`, 2026-07-20 — real ESPN season line, mapped
      to the league's roto categories; live-verified Cam Thomas vs Cooper Flagg)
- [ ] Optimizer produces a correct, explained daily lineup in dry-run
- [ ] A `propose` team: DM'd proposal → owner approves → lineup set on Yahoo + logged
- [ ] An `auto` team: lineup set within guardrails, after-action report sent
- [ ] Guardrails demonstrably block a disallowed move (never-drop / volume / FAAB cap)
- [ ] Daily lineup management runs on schedule per each team's mode

## Notes

### 2026-07-20 — P1 DONE: `fantasy_player_value` (valuation) live + eval-gated
Player valuation against the league's own scoring, from real stats. What landed:
- **`wes_nba.py` season-stats layer**: `athlete_id(name)` resolves an NBA player
  via ESPN's site search (filtered to `/nba/player/` links — drops NFL/college
  namesakes), `player_season_stats(name)` fetches the ESPN athlete-stats endpoint
  and `parse_season_stats()` normalizes the LATEST season into a dict —
  `{name, season, gp, min, cats:{PTS,REB,AST,ST,BLK,TO,DD,TD,EJCT,…}, counting}`.
  Per-game for the rate cats (`averages`), season totals for DD/TD/EJCT
  (`miscellaneous`). Offseason-safe: the endpoint returns the last completed
  season. Traded-in-a-season players: takes the max-GP (combined) row.
- **`pc/wes_fantasy.py`** — the valuation/decision **engine** (design §2's
  ingestion→valuation split; the P2 optimizer lands here too). `player_value`
  maps a stat line through the league's categories and formats a compact line
  the model reads; `fantasy_player_value(player, versus=)` resolves the league's
  categories from the configured team (via `wes_yahoo.league_categories`, cached;
  falls back to the standard roto set) and supports a head-to-head compare.
- **Registered in `wes_server.TOOLS`** (now 14 tools) + dispatched; description
  forbids inventing stat lines.
- **Live-verified** (Discord): "who's the better play, Cam Thomas or Cooper
  Flagg?" → the model called `fantasy_player_value` with both, got real 2025-26
  lines, and reasoned from the actual numbers (REB 6.7 vs 1.7, double-doubles) —
  no invented stats. Matches the P1 acceptance.
- **Tests:** new `test_unit_fantasy.py` (parser + name→id resolver on a saved
  ESPN fixture + engine formatting/degradation) + server registration/dispatch;
  264 unit pass. Golden case `fantasy-value` (anti-hallucination; free ESPN feed,
  runs nightly). Eval-gated.

**P1 scope note / next:** valuation is the **category line**, not a z-score /
replacement-value ranking — that needs the whole-league player pool (a population
fetch) and belongs with the optimizer. **Next: P2** — daily lineup optimizer
(today's games + injuries + slot eligibility → optimal active lineup, explained,
shadow-soaked). Needs the offseason-blank *eligible positions* (P0 caveat), so
re-verify positions in-season via the `WES_YAHOO_LIVE=1` canary before P2 relies
on them.

### 2026-07-20 — P0 DONE: `fantasy_my_team` live + eval-gated
The read path is now a live tool. What landed:
- **`wes_yahoo.fantasy_my_team(team=None)`** — the P0 read entry point. Resolves
  a team from `teams.yaml` (by name, or the first/default), scrapes its live
  roster + league scoring, and combines them into one compact block. Degrades to
  a string on any problem; when no `teams.yaml` exists yet it falls back to a
  live `my_teams()` listing + a setup hint. Plus a small team registry
  (`_load_teams`/`configured_teams`/`_resolve_team`, `WES_FANTASY_TEAMS`,
  default `~/wes-pc/teams.yaml`).
- **Registered in `wes_server.TOOLS`** (now 12 tools) + dispatched in `run_tool`;
  `import wes_yahoo` added. Description forbids inventing players/stats.
- **PC-local `teams.yaml`** created at `C:\Users\awarm\wes-pc\teams.yaml` (NOT in
  the repo — names the real league): team **"Enemies of Whiffer"**,
  `team_key nba.l.114020.t.1`, `league_key nba.l.114020`, `autonomy: propose`.
- **Live-verified end to end** via `wes-dev.ps1 say discord`: "who's on my
  fantasy team + scoring categories" → the real 15-man roster (Maxey, Harden,
  Flagg, Luka Dončić, Vučević…) organized by slot + roto cats
  (PTS/REB/AST/ST/BLK/TO/EJCT/DD/TD). No hallucination.
- **Tests:** +11 unit tests (team registry + `fantasy_my_team` in
  `test_unit_yahoo.py`; registration + dispatch in `test_unit_server.py`) — 159
  pass. New golden case `fantasy-roster` guards against hallucinated rosters
  (accepts a real roster OR a graceful "needs connecting"). Eval-gated per
  #026/#027 tool-count budget.
- **Offseason caveat still holds:** player NBA-team + eligible positions render
  blank (only slot shows); names/slots/status/ids all present. The P2 optimizer
  needs eligible positions, so re-verify in-season via the `WES_YAHOO_LIVE=1`
  canary. `fantasy_my_team` also does NOT yet cover matchup/standings (scrapers
  unwritten) — deferred to a later phase; the P0 gate is roster+scoring.

**Next: P1** — valuation + full stat lines (`fantasy_player_value`), extending
`wes_nba.py` to full box-score lines mapped to this league's roto categories.

### 2026-07-19 — P0 read WORKING (live-verified against the owner's real league)
Browser automation is proven end to end. Setup that landed:
- Playwright installed in the **WES venv** (`C:\Users\awarm\wes-pc\.venv`), not
  just conda base — the server runs in the venv. Real Chrome via `channel=chrome`.
- **Google SSO block solved:** the owner's Yahoo uses "Continue with Google,"
  which blocks automated browsers. Fix = drive real Chrome +
  `--disable-blink-features=AutomationControlled` + drop `--enable-automation`
  (see `_Session`). Bundled Chromium alone gets blocked; real Chrome passes.
- **Auth detection = cookies, not DOM.** `logged_in()` checks Yahoo's `A1/T/Y`
  cookies. The earlier DOM heuristic false-negatived (signed-in pages still carry
  a hidden `login.yahoo.com` link). `yahoo_connect.py` now re-logs-in a
  signed-out profile instead of skipping.
- **Scrapers written + verified** against the live roster/settings pages:
  `table.ysf-rosterswapper` rows → name (`a.name`), slot (`span.pos-label
  [data-pos]`), status (`span.player-status`), player id (from `/players/<id>`).
  Scoring from the settings body text. Team name from the team-page `<title>`.
- **Offseason caveat (real):** each player's NBA team + eligible positions render
  blank right now (Yahoo only fills them in-season); name/slot/status/id always
  present. The optimizer (P2) needs eligible positions, so that part must be
  re-verified once the season opens — the `WES_YAHOO_LIVE=1` canary
  (`WES_YAHOO_TEST_TEAM=nba.l.<league>.t.<team>`) is the drift check.

**Common failure while iterating:** stray Chrome procs holding the profile lock
(`ProcessSingleton`, error 32). Kill `chrome.exe` whose cmdline has
`wes-pc\yahoo_profile` and remove `yahoo_profile\SingletonLock` before relaunch.

**Remaining for P0:** register `fantasy_my_team` in `wes_server.TOOLS` (was
deferred to avoid a dead tool spending router budget — now it answers for real).
That touches the LIVE router, so gate it behind an `eval_turns.py` run (#026/#027
tool-count budget). Then P0 is done and P1 (valuation + full stat lines — the
roster table already exposes per-cat columns) begins.

### 2026-07-17 — Official Yahoo API ABANDONED; pivot to browser automation
Owner research + a r/fantasyfootballcoding thread (owner screenshot) settled it:
Yahoo now gates **all** Fantasy API access — read included — behind a manual
application + a **DocuSign** (a community dev reported **~3 weeks** to approval,
**read only**, write still pending). The agreement's terms **forbid
storing/caching Yahoo data** (delete within 30 days). That is incompatible with
this system on two counts: (1) the whole architecture is caching-first (design
§8.3/§8.7, the #027 P2 all-team cache), and (2) applying for approval for an
autonomous **write** bot that caches league state would likely violate the
agreement outright.

**Decision:** keep Yahoo (owner's league is there) but reach it the way a person
does — a persisted logged-in **Playwright** session, scripted. Full rationale +
approach in **design §1 (decision record) and new §10**. Key points:
- Only the **ingestion + execution adapter** changes; engine, rails (§5),
  autonomy config (§4), optimizer are untouched.
- **Deterministic script, LLM never free-drives the page** — optimizer picks the
  move, Playwright replays it, so §5 rails still gate every write pre-click.
- P0 is **no longer blocked on credentials** — there are none; it's now a
  build task (write the scraper against the real logged-in UI).
- New risks to handle: selector drift (parse canary), session/2FA expiry,
  bot-detection (headed, low-volume, human-paced).

### 2026-07-17 — scaffold cleanup + Playwright stub DONE
The OAuth scaffold is retired and the browser adapter is stubbed (13 unit tests
pass, 1 live canary skipped):
- `pc/wes_yahoo.py` — rewritten as a **Playwright persistent-profile adapter**.
  Kept verbatim: `format_roster`/`format_scoring` + the normalized-dict contract.
  New: `_Session` (persistent context on `WES_YAHOO_PROFILE_DIR`), `login()`
  (one-time headed sign-in), `_scrape()` (degrades to a string on any failure).
  **STUBS** (`_extract_roster`/`_extract_scoring`/`_extract_my_teams`) raise
  `NotImplementedError` — their selectors need the real logged-in UI.
- `pc/yahoo_connect.py` — rewritten from OAuth-consent to the browser sign-in
  flow (install Playwright → open Yahoo login → persist profile).
- `test_unit_yahoo.py` — dropped OAuth/token/scope + Yahoo-JSON-shape tests;
  kept formatter tests (build normalized dicts directly), added degradation +
  key-helper tests. Live canary reason updated (needs profile AND scrapers).
- `fantasy/teams.example.yaml` — `token_ref` → `profile_ref`; token wording gone.

**Remaining for P0 (needs the owner + the live UI):** `pip install playwright`
+ `playwright install chromium` on the PC venv, run `python pc\yahoo_connect.py`
to sign in once, then write the three DOM extractors against the real pages and
turn on the `WES_YAHOO_LIVE=1` canary. Only then wire `fantasy_my_team` into
`wes_server.TOOLS`.

Everything below this line predates the pivot — kept for history, superseded above.

### 2026-07-16 — P0 scaffold built; BLOCKED on owner credentials
Everything that doesn't need live creds is in:
- `pc/wes_yahoo.py` — OAuth2 (consent + **rotating**-refresh-token handling),
  authenticated fetch w/ 60s cache, roster/scoring parsers, compact formatters.
  Degrades to a string on any failure; never raises into a turn.
- `pc/yahoo_connect.py` — the one-time consent CLI (can't be automated: needs a
  human signed into Yahoo in a browser).
- `fantasy/teams.example.yaml` — per-team autonomy + guardrails config (§4).
- `tests/test_unit_yahoo.py` — 23 pass, network-free, + a `WES_YAHOO_LIVE=1`
  schema-drift canary.

**Blocked on the owner** (cannot be done for them):
1. Register a Yahoo app (`developer.yahoo.com/apps/create`) — name anything,
   Homepage blank, Redirect `oob`, **Confidential Client**, **check no API
   permissions**.
2. `setx WES_YAHOO_CLIENT_ID/_SECRET`, then `python pc\yahoo_connect.py` and
   approve in a browser.
3. Paste the resulting `team_key` into a PC-local `teams.yaml`.

### 2026-07-16 — Yahoo's app form has CHANGED (verified against the live form)
Earlier instructions here were written from Yahoo's older form and were wrong.
The current form (owner screenshot) shows:
- **No "Installed Application" app type** — it now asks *OAuth Client Type:
  Confidential vs Public Client*. Confidential is correct (we hold a secret and
  authenticate with HTTP Basic).
- **No "Fantasy Sports" API permission** — the only options are *OpenID Connect
  Permissions* and *TW Auction*. Neither is wanted.

Consequences, both handled in `wes_yahoo.py`:
- The fantasy grant must be requested at **authorize time** via `scope`
  (`WES_YAHOO_SCOPE`, new). `authorize_url()` now sends it; without it the token
  has no fantasy access and every call 401s. **Defaults to `fspt-r` (read) on
  purpose**: P0-P2 never write, so a read-only grant makes "the shadow-mode soak
  cannot write" a guarantee enforced by *Yahoo*, not just by our executor
  (design §5). P3 re-consents with `fspt-w`.
- **`oob` is now uncertain** — it was tied to the retired Installed Application
  type. If Yahoo rejects it, register a `https://localhost/...` redirect, set
  `WES_YAHOO_REDIRECT`, and replace the paste-the-code CLI with a local
  listener. Unresolved until the owner submits the form.

**Risk to watch:** if Yahoo has withdrawn Fantasy API access for *new* apps
(not just hidden the checkbox), `scope=fspt-r` will be rejected at consent and
the platform choice in §1 needs revisiting — ESPN (fragile writes) or Sleeper
(no writes) would both materially change the epic. First consent attempt tells
us; do not build further on Yahoo until it succeeds.

**Deliberately NOT done yet:** the `fantasy_my_team` TOOL is not registered in
`wes_server.TOOLS`. Router tool-count is already a live constraint (#027), so
adding a tool that can only answer "not connected" would spend budget for zero
capability and risk an eval regression. Wire it the moment creds exist.

**SCHEMA CAVEAT:** Yahoo's JSON (positional arrays + `{"0":..,"count":N}`
pseudo-arrays) is parsed by *searching* for keys (`_walk`), not by position —
but the fixtures encode Yahoo's *documented* shape, unverified against a real
payload. Expect the first live run to correct them; that's what the canary is for.

### 2026-07-16 — origin
- filed at owner request to turn the NBA data work (#027) into a full
  autonomous-GM initiative. Offseason (season tips October) is deliberately the
  build window so read/valuation/optimizer plumbing is shadow-tested before
  writes matter. Next concrete step: spin out **#030 = P0 (Yahoo read)** and
  prototype the OAuth flow. Design doc is the source of truth; keep this ticket's
  phase checklist in sync as sub-tickets close.
