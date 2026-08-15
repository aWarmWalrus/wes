---
id: 039
title: Sleeper as a second fantasy platform
status: open
priority: high
created: 2026-08-14
closed:
tags: [fantasy, nfl, sleeper, platform]
related: ["#029", "#034", "#035", "#037", docs/fantasy-gm-design.md]
---

## Problem / Goal

The owner has a second NFL league on **Sleeper** — *"Alloy Agents vs. Humans"*,
league `1393935116232818688`, a for-fun agents-versus-humans league — and wants
the same tools and automations to work there, **full-auto**.

Everything in the fantasy stack currently assumes Yahoo: `wes_execute` and
`wes_fantasy` call `wes_yahoo` directly, and `teams.yaml` entries are
Yahoo-shaped.

## Recon — verified live 2026-08-14

**League** (`GET /v1/league/<id>`): 12 teams, 2026, status **`pre_draft`**,
snake draft, 15 rounds, `cpu_autopick` on. Slots
`QB RB RB WR WR TE FLEX FLEX K DEF` + 5 BN. FAAB waivers, $100 budget, trade
deadline week 11. Owner is `awarmwalrus` → **roster_id 3**.

**Sleeper is a far kinder integration than Yahoo**, and the differences are
structural, not cosmetic:

| | Yahoo | Sleeper |
|---|---|---|
| Auth | logged-in browser profile | none |
| Reads | Playwright + DOM scraping | documented JSON API |
| Scoring | text scraped off a settings page, section-aware parser | 43 numeric settings as JSON |
| Player identity | fuzzy name matching | `espn_id` / `gsis_id` / `yahoo_id` in the feed |

**The identity problem is simply absent.** `/v1/players/nfl` is 14MB / 12,218
players in 0.7s and carries `espn_id` (6,736 players), `gsis_id` and `yahoo_id`
— so a Sleeper player joins to our ESPN valuations, and to nflverse (#036), BY
ID. Proven end to end: 121 free agents joined to the ESPN pool by id and ranked
under this league's scoring (McCaffrey 24.51 pts/g, full PPR).

**Note the leagues score differently** — Yahoo is half-PPR, Sleeper is full PPR
(`rec: 1.0`). Per-league scoring was already read per league, so this is
handled, but it means cross-league player values are NOT comparable.

## What is built (2026-08-14)

`pc/wes_sleeper.py`, **read-only**, 33 tests, all network-free:

- `parse_scoring` — Sleeper's 43 settings → our `{weights, tiers, unknown}`,
  the same contract `wes_fantasy.nfl_league_scoring` returns, so the valuer,
  optimizer and executor cannot tell the platforms apart.
- `parse_roster_slots` — `FLEX` → `W/R/T`, `SUPER_FLEX` → `Q/W/R/T`, order
  preserved (Sleeper aligns `starters` POSITIONALLY to this list).
- `parse_players` / `players_index` — the 14MB dump → a slim id index, cached
  6h in-process and fetched with `ttl=0` so the raw body is never pinned in the
  shared HTTP cache.
- `parse_roster` — Sleeper stores no slot on a player; it is implied by index in
  `starters`, and `"0"` means an EMPTY slot rather than a player.
- `free_agents` — Sleeper has no free-agent endpoint; availability is the
  COMPLEMENT of every roster.
- `find_roster_id` — display name → roster id, so registering a league isn't a
  manual id hunt.

The `unknown` field earned its keep on first contact: it surfaced `fgm_60p`
(now mapped to `FG50`; leaving it would undervalue a big-leg kicker) and `ff`
(forced fumbles — kept honestly unknown, since we have no stat to score it).

## THE OPEN QUESTION: writes

**Sleeper's public v1 API is read-only.** There is no documented endpoint for
setting a lineup or making a transaction, so *full-auto is not yet possible* and
`wes_sleeper` deliberately contains no write path — half a write is worse than
none.

Probed (non-mutating): `sleeper.app/graphql` exists and answers unauthenticated
(`{"data":{"__typename":"RootQueryType"}}`). Mutations will require an account
token. Options, none yet chosen:

1. **Internal GraphQL + session token.** Likely how the app itself writes.
   Undocumented, ToS-grey, and it can change without notice — but it would be a
   clean HTTP write path with no browser.
2. **Playwright browser automation**, as with Yahoo. Known-good pattern in this
   codebase, heavier, and the DOM drifts.
3. **Advise-only on Sleeper.** Full read + recommendations, owner executes.
   Loses "full-auto" but costs nothing and is available today.

Worth weighing against the fact that this is explicitly a **for-fun league** —
the same reasoning that made `nfl.l.957011` the auto soak target applies here,
so the risk appetite is high. The constraint is capability, not caution.

## Write path: BROWSER AUTOMATION (owner decision, 2026-08-14)

Playwright, as with Yahoo — not the internal GraphQL. Recon confirmed the shape:

- `sleeper.com/leagues/<id>` and `/team` **bounce a signed-out request to
  `/?redirect=...&login=`** rather than erroring. That matters more than it
  sounds: a logged-out scrape returns a perfectly valid page *about something
  else*, which would parse as an empty roster. `_is_login_wall` detects it
  explicitly.
- The **draft room is publicly viewable** without login (it already renders
  "12-team PPR Snake, 15 Rounds, 10 Min Timer" and the full pick grid), though
  joining a draft needs an account.

Built: `_Session` (its own persistent profile, separate from Yahoo's so a
re-login on either cannot disturb the other), `logged_in()`, and
`pc/sleeper_login.py` for the one-time interactive sign-in. Verified against the
real signed-out profile: correctly reports not-signed-in.

**The gestures themselves are NOT built, and cannot be yet.** The league is
`pre_draft` with 0 players on every roster, so the lineup and add/drop controls
do not exist on the page to recon. Writing them from guesswork is precisely how
the Yahoo swapper benched the wrong player (#029) — that recon waits for a real
roster.

### The session is SOLVED — via token, not login (2026-08-14)

The interactive login is a dead end: sleeper.com's form sits behind **hCaptcha**,
which is built to detect exactly the browser Playwright launches (ours fails its
checks — `window.chrome` absent, `plugins: 0`, and a `HeadlessChrome` UA when
headless). The owner could not clear it by hand, and one success would not have
lasted anyway, since hCaptcha re-challenges.

**But the captcha guards OBTAINING a session, not PRESENTING one.** Injecting an
existing account token into localStorage walks straight past it, and the app then
bootstraps its own session state as though a human had signed in.

- Token lives in `WES_SLEEPER_TOKEN` (PC user environment, never the repo, same
  convention as `WES_DISCORD_TOKEN`).
- `TOKEN_KEY = "token"` — **pinned by testing candidates ONE AT A TIME against a
  cleared store**. Of `token`, `user_token`, `auth_token`, `jwt`,
  `access_token`, only `token` gets through. Injecting all five also worked, but
  shipping that shotgun would have kept "working" if the real key changed, right
  up until it didn't.
- `authenticate(page)` loads the origin FIRST — localStorage is per-origin, so
  writing it from `about:blank` lands nowhere, silently.
- Verified live: `session : OK — signed in; team page loaded`.

`pc/sleeper_login.py` is now a session *check* rather than a login flow.

**And the lineup mechanism is now visible.** The authenticated team page says
*"Click on position buttons to update your lineup"* — the same badge-click
pattern as Yahoo, which means the known trap applies: two slots of the same type
(RB/RB, WR/WR, and two FLEX here) cannot be told apart by slot type, so any
write must target by PLAYER, never by slot. That is the mistake that benched the
wrong player on Yahoo (#029).

## Draft: the read path needs no auth at all (2026-08-14)

The captcha blocks *getting a session*, not *reading*. Sleeper's draft
endpoints are public, so **who is gone, whose turn it is, and who to take are
all solvable today** — only SUBMITTING a pick needs the session.

Built and verified live against the real draft:

- `draft`, `draft_picks`, `drafted_player_ids` — picks matched by `player_id`,
  never by name. Sleeper hands us the exact id, and name matching is how a draft
  bot recommends someone already taken.
- `wes_draft.slot_for_pick` / `next_pick_for_slot` / `picks_until_turn` — snake
  arithmetic, including Sleeper's `reversal_round` option. "20 picks to wait"
  and "1 pick to wait" are different strategies, so this is load-bearing.
- `wes_draft.targets_from_slots` — derived from the league's own slots, so an
  unusual league works without editing a table.
- `wes_sleeper.draft_board` — the live recommendation.

Real output, 2 picks before the owner's turn at slot 3 of 12:

```
Round 1, 0 picks in — 2 picks until your turn (slot 3 of 12).
  1. Christian McCaffrey (RB, SF) — 18.62 over replacement (24.51 pts/g) +4 need
  2. Jonathan Taylor (RB, IND) — 15.43 over replacement (21.32 pts/g) +4 need
  3. Davante Adams (WR, LAR) — 11.3 over replacement (15.92 pts/g) +4 need
```

**Two modelling bugs were caught by looking at real output, not by tests.**

*Flex folded into every position.* Adding both FLEX slots to each eligible
position produced `TE: 3`. Nobody starts three tight ends, and a need bump built
on that keeps recommending one. Flex is capacity for ONE of several positions,
not for each, so it is now tracked separately and applied as a weaker shared
bump.

*Raw points instead of value over replacement.* The first working board put
**six quarterbacks in the top eight**. A QB out-scores a back in most systems
but a 12-team league starts 12 QBs and ~36 RB/WR, so the 12th-best QB is nearly
as good as the best while the 36th receiver is far worse than the 5th. What a
pick is worth is the gap to the player you could have had anyway.
`replacement_levels` fixed it; the board is now RB/WR-led, as a round-1 PPR
board should be, and Josh Allen (22.04 raw) correctly falls out of the top 8.

**Implication for sequencing:** with a 10-minute pick timer, a recommendation
relayed to a human is most of the value of an autodrafter, and `cpu_autopick` is
the safety net if nobody is watching. So the draft is *useful before the write
problem is solved*, and auto-submit is an increment rather than a prerequisite.

**A mock draft is the obvious rehearsal.** Sleeper offers them, they create a
real draft with real picks, and that is the only way to (a) see a live pick
record shape — `/picks` has returned `[]` every time so far — and (b) recon the
draft-room DOM for auto-submit, without waiting for ~Sept 6.

## Mock draft created — and the draft room resists synthetic clicks (2026-08-15)

Created a mock via `/draftboards` -> "NEW MOCK NFL DRAFT": draft
`1394249532753080320`, private 10-team snake, 2 min/pick, 15 rounds,
`cpu_autopick` on. The public API reads it, so the read path is confirmed
against a draft we own.

**Control shape (same trap as Yahoo).** Every label is a text div nested inside
the element that carries the handler:

```
div.start-draft-text   <- the words; clicking this does NOTHING, silently
  div.start-draft-button   <- the actual control
div.claim-text  ->  div  ->  div.header-button   (inside div.draft-user-header)
```

**~~But driving them does not work.~~ CORRECTED 2026-08-15 — the click DID
work.** I concluded the app ignored synthetic clicks after `.start-draft-button`
appeared to do nothing. The owner spotted the actual cause: **it opens an "are
you sure you want to start the draft?" confirmation dialog**, which I never
looked for. The click landed; I just never handled the modal.

Recorded because the wrong conclusion was the expensive part: it would have
condemned the whole Playwright approach on a false negative. **A control that
appears not to respond deserves a check for a modal before it earns a verdict
about the platform.**

**The reframe that keeps this cheap:** claiming a seat and starting a draft are
ONE-TIME HUMAN actions that never needed automating. The only gesture that must
work unattended is MAKING A PICK while on the clock — a different screen, with
different controls, unreachable until a draft is actually running. So the
lobby's resistance may simply not matter.

**Next step is 30 seconds of owner time**: claim a seat and press START DRAFT at
`https://sleeper.com/draft/nfl/1394249532753080320`. That unblocks three things
at once — verifying pick parsing against real records (`/picks` has returned
`[]` every time so far), reconning the on-the-clock pick control, and finding
out whether THAT control takes a synthetic click, which is the question that
actually decides auto-draft.

## Remaining work

- [ ] **Platform dispatch.** `teams.yaml` gains `platform: yahoo|sleeper`, and
      the engine resolves an adapter instead of importing `wes_yahoo`. The read
      seam is small: `roster_players`, `free_agents`, league scoring/slots,
      `_resolve_team`. The WRITE seam is deeply Yahoo-shaped (`_Session`, URL
      building, DOM) and is what actually needs abstracting.
- [ ] Recon the lineup/add-drop gestures against a REAL roster (mock draft, or after ~Sept 6). The session is no longer the blocker; an empty roster is.
- [ ] Register the league in `teams.yaml` — **deliberately not done yet**: the
      league is `pre_draft` and every roster has 0 players, so a GM cycle would
      have nothing to manage and would only add noise.
- [ ] The **draft** is the first real event (snake, 15 rounds, `cpu_autopick`
      on). #030 shelved Yahoo draft automation because the owner drafts
      manually — worth re-asking here, since this is an agents-vs-humans league
      and Sleeper has a documented draft API.
- [ ] Sleeper's own signals we already know are useful: `trending/add` (crowd
      momentum, verified working during #036 recon) and FAAB waiver budgets —
      the latter is the `max_faab_bid_pct` guardrail that is still enforced by
      no code.

## Notes

**2026-08-14 — sequencing.** Reads first because they are unblocked, useful on
their own (recommendations), and independent of however writes get resolved.
Nothing about the read adapter has to change if the write path turns out to be
GraphQL or Playwright.

`pre_draft` also means there is no urgency on the management loop but there IS a
deadline on the draft: `start_time` 1788552000000 (~2026-09-06).


## Live mock draft — what running one actually taught us (2026-08-15)

The owner started the mock by hand. `status: drafting`, real picks flowing.
Three things came out of it that no amount of offline reasoning would have.

**1. `roster_id` is null in mock drafts.** The real pick record:

```json
{"pick_no": 1, "draft_slot": 1, "roster_id": null, "player_id": "9509",
 "metadata": {"first_name": "Bijan", "last_name": "Robinson", ...}}
```

`draft_board` identified "my picks" by `roster_id`, so in a mock it matched
NONE of them — and the positional-need bump was computed against an empty
roster while the output looked perfectly healthy. Now keyed on `draft_slot`,
with `roster_id` kept as a fallback for league drafts where it is populated.

**2. Sleeper has a QUEUE with AUTO-PICK.** The live room exposes
`draft-queue` / `queue-action` / `AUTO-PICK` alongside the `draft-button`.
Players are ranked into a queue and Sleeper drafts from it when you are on the
clock.

~~**This should be the auto-draft design, not on-the-clock clicking.**~~
**OVERRULED by the owner, 2026-08-15, and correctly.** I argued the queue was
"strictly more robust". That overstated it: robustness is not the only axis, and
a pre-committed ordering *cannot express the things that make a pick good*.
Owner: *"there is some decision making around team fit, making sure bye weeks
are staggered, not having too many players from same team... I know it's hard
but we do hard things."*

A queue is fixed before the draft and cannot know what fell to you — it cannot
say "I now need a bye that isn't week 9" or "that's my third Bengal". So:
**live decision-making is the target; the queue is a SAFETY NET underneath it**,
kept populated so a missed clock still drafts something sane.

**3. The board works on live data.** Mid-draft, on the clock at slot 6 of 10,
with the five already-drafted players correctly excluded:

```
Round 1, 5 picks in — you're ON THE CLOCK (slot 6 of 10).
  1. Derrick Henry (RB, BAL) — 10.55 over replacement (16.44 pts/g) +4 need
  2. Davante Adams (WR, LAR) — 9.94 over replacement (15.92 pts/g) +4 need
```

**Caveat worth carrying:** `draft_board` takes a `league_id` for scoring, and a
mock draft has no league — this run scored a Standard mock under the real
league's PPR settings. Fine for a test, wrong in general; the scoring source
needs to come from the draft when there is no league behind it.


## Roster construction constraints (2026-08-15)

Value over replacement says who is BEST. These say who FITS. A board that knows
only value hands you three Bengals and four players on the week-9 bye, and is
arithmetically right while losing you the week.

Two different KINDS of rule, deliberately kept apart:

- **Same-team stacking is a HARD cap** (`DEFAULT_MAX_PER_NFL_TEAM = 3`). It is
  countable, so it is a constraint rather than a judgement (#038's distinction),
  and at the cap the player is excluded outright — "one more Bengal" is not a
  matter of degree once the correlation risk is taken.
- **Bye clustering is a SOFT penalty**, growing with the pile-up. Some overlap
  is unavoidable and harmless; a hard rule would refuse good players for a small
  real cost.

Penalties are denominated in value-over-replacement, so they trade against it
honestly, and each returns a REASON so a pick can explain itself.

**Bye weeks had to be derived.** No platform we read supplies one — Sleeper's
player object has 40-odd fields and none is the bye. `pc/wes_schedule.py` gets
them from nflverse's schedules release: a bye is the regular-season week a team
does not appear in. Derived rather than looked up, so it survives the league
changing the number of weeks. A team with no missing week is OMITTED, never
defaulted — an unknown bye must not read as "no bye", or a board would stack a
whole roster onto one week believing it had staggered them.

Verified against the completed mock's slot-6 roster (15 real players, which had
3 on the week-7 bye and 3 on week-11):

```
Samuel Womack (NYJ): -1.75; would be your 3rd from NYJ; 3 players on the week-13 bye
Geno Stone    (BUF): -2.5;  4 players on the week-7 bye
```

**Also harvested from the player dump, unused so far but relevant to #036:**
`depth_chart_order`, `depth_chart_position` (the depth-chart data #036 wanted,
sitting in a feed we already fetch) and `search_rank` (Sleeper's own consensus
ranking — a free market signal).

**Still to do for live drafting:** the pick submission itself. The mock
completed (150 CPU picks) before its controls could be reconned, and
`.draft-button` only exists while a draft runs — so that needs another mock.
Note the confirm-dialog lesson applies: expect a modal after the click.


## Agentic drafting (2026-08-15)

Owner: *"I want this to be handled agentically."* Fair — what existed was an
ALGORITHM. `draft_board` computed value over replacement, positional need and
roster-fit penalties, then stopped at an ordered list. Nothing about it was
reactive or judgment-driven.

### The safety property had to change, and that is the interesting part

#038's rule is **"the LLM may SUBTRACT, never ADD"** — it can veto a roster move
but never originate one, so its worst case is inaction. **Drafting breaks that
rule outright, because a pick is MANDATORY.** The clock expires, `cpu_autopick`
takes it, and "do nothing" does not exist. A model that may only veto cannot
draft at all.

So the property is preserved a different way:

```
the ENGINE constrains the choice set   ->   the MODEL chooses within it
```

Every shortlisted candidate is already verified available BY ID, legal under the
hard same-team cap, and actually valued. The model picks one of N and never
names a player freely. That keeps what #038 is really protecting — **the
model's output is a choice among verified options, not an unverified
assertion** — the same reason `approve={drop,add}` is re-checked against the
live recommendation rather than trusted.

A returned key that is not on the shortlist is discarded in favour of the
engine's top pick. **A hallucination cannot become a pick.** Nor can a stale
board: if someone took that player while we were thinking, the key no longer
matches and we fall back.

### Built

- `wes_sleeper.draft_candidates` — the state and shortlist as STRUCTURED data.
  Split out of `draft_board` because an agent handed formatted prose would have
  to parse back out what the code already knew, and #034 puts the decision layer
  below the model layer. `draft_board` now formats this.
- `wes_draft_agent.choose` — model picks one, validated against the shortlist,
  falling back to the engine on a bad key, an unparseable reply, or no model at
  all.
- **`source` ("model" or "engine") is recorded, not hidden.** An agent whose
  judgment silently degrades to a sort is one you cannot evaluate later; "was
  the model right the twelve times it disagreed with the board?" has to stay
  answerable. It also surfaces in the reply as "(engine fallback)" so a
  degraded pick does not read as a considered one.
- The shortlist sent to the model carries the fit concerns, so it can weigh
  bye clustering and stacking rather than re-deriving what it cannot see.

11 agent tests, all model-free — the injected call tests the SAFETY property
rather than the model's taste.

### Still missing before this drafts unattended

1. **Pick submission.** `.draft-button` only exists while a draft runs, and the
   mock completed (150 CPU picks) before it could be reconned. Expect a confirm
   modal, per the START DRAFT lesson.
2. **The loop.** Poll draft state, act inside `PREPARE_WITHIN_PICKS`, re-verify
   availability immediately before submitting, confirm the pick landed by
   reading `/picks` back — the same "verify after write" discipline as the
   Yahoo executor.
3. **Timing rails.** Decide well inside the clock and stand down if late:
   `cpu_autopick` is a perfectly good fallback, and racing the timer risks a
   half-submitted pick, which is worse than an autopick.


## The pick gesture, captured live (2026-08-15)

Second mock (`1394383922141364224`), started by the owner. The live room:

```html
<div class="player-rank-item2 TE show-watchlist-action odd">
  <div class="draft-button-wrapper">
    <div class="draft-button disable"><svg/></div>   <- PER ROW; `disable` = not your turn
  <div class="rank">114</div>
  <div class="name col-border-right"><div class="name-wrapper">Brenton Strange…
```

Also present: a `Find player Ctrl + U` search input, `[class*=timer]` showing
`02:00`, and `[class*=current-pick]` showing `4.2 02:00` — so the clock and the
current pick are readable from the DOM as well as the API.

**Every row has its own `.draft-button`.** "Click the draft button" would draft
whoever happens to sit at the top of a re-sorting list — the Yahoo swap bug
wearing a different hat. So `submit_pick` targets the ROW belonging to a named
player and never a bare control.

**THE ID/NAME SEAM.** The engine reasons in `player_id` — exact, and the id
Sleeper hands us on every pick. **The draft room's DOM contains no player id
anywhere.** So the click must be matched by NAME, and names are the ambiguous
thing (suffixes, punctuation, shared names). That gap is closed the only honest
way: read the pick back from the API and confirm the id that actually got
drafted is the id we intended. `_norm_name` handles the ordinary cases
("A.J. Brown" / "AJ Brown", "Ja'Marr" / "JaMarr", "Jr." / "Jr").

`submit_pick` refuses rather than guessing when: the player is not listed, the
button carries `disable` (not on the clock — clicking would do nothing and we
would report a pick that never happened), or the verification does not find our
id in the picks. It shares #029's kill switch rather than inventing a second
one.

**Not yet exercised against a live clock** — the button is disabled until it is
your turn, and both mocks completed without one coming round while a script was
watching. That is the remaining unknown, along with whether this click opens a
confirm modal like START DRAFT did (the code assumes it might).


## Replay harness — and what it says about the judgment layer (2026-08-15)

`tests/draft_replay.py` rebuilds the board as it stood at each of our picks in a
COMPLETED draft (via `draft_candidates(_picks=history)`) and runs every
contender over the identical shortlist. Offline, repeatable, no live draft, no
risk. The engine's own top pick is a contender on purpose, because the question
worth answering is not "12b or Claude" but **"does LLM judgment beat the sort at
all?"**

Result over 15 picks of the completed mock:

```
local (gemma4:12b) agreed with the engine on 13/15 (87%)
claude (haiku)     agreed with the engine on 12/15 (80%)
```

**The model choice barely matters, and that is the finding.** Read the reasons
rather than the percentages: nearly every one is a restatement of the input —
*"offers the highest value-over-replacement"*. Neither model is contributing
judgment; both are rubber-stamping the sort. The judgment layer is not currently
earning its cost, and swapping models would not change that.

**And the harness surfaced something worse about the BOARD.** Tyreek Hill and
George Kittle sit on top of our shortlist at picks 75, 86, 95, 106, 115, 126,
135 and 146 — the CPU passed on them for over a hundred picks. Either we have
found a market-wide inefficiency, or our valuations are wrong. It is the second:
these are **2025 season aggregates**, and Sleeper's own rankings have moved on.
This is #036 showing up as concrete bad advice rather than a theoretical gap:
*a chooser cannot be better than the board it chooses from.*

So the ordering is clear: **fix the valuations before tuning the chooser.** A
better model on a stale board is a more articulate wrong answer.

**CORRECTED 2026-08-15 — the fix is NOT #036.** I pointed at weekly projections;
the owner pushed back, correctly: *"weekly projection should not affect draft
strategy."* A draft is about expected SEASON-LONG production. #036 is weekly
matchup adjustment for in-season lineup calls. Different problem, different data,
and conflating them would have built the wrong thing.

The real defect is narrower: **we value players by 2025 ACTUALS as a proxy for
2026 expected value.** There is no forward-looking season projection in the board
at all — which is exactly why it still loves last year's producers.

The data exists and is free:

```
ESPN kona:  PROJ season=2026 split=0 scoringPeriod=0 total=336.12   <- season total
            draftRanks: {'STANDARD': 3, 'PPR': 3, 'SUPERFLEX': 9}   <- market view
Sleeper:    search_rank — Bijan 1, Gibbs 1, Chase 3, Nacua 4        <- market view
```

`statSourceId=1` + `statSplitTypeId=0` is a full-season 2026 projection, in the
same endpoint #036 already found for weekly numbers. The draft board should be
valued on that, with `search_rank`/`draftRanks` available as a market
sanity-check — a player our board loves and the market has abandoned is a signal
to distrust the valuation, not a bargain.

**One real bug came out of it too.** Every Claude call fell back with "model
unavailable" — but Claude had answered perfectly well and wrapped its JSON in a
markdown fence, which `json.loads` rejected. Ollama's `format=json` guarantees a
bare object; the Anthropic API does not. `_strip_fence` handles it, and the
fallback message now says "no usable reply" rather than claiming unavailability,
because the original wording sent the diagnosis in the wrong direction entirely.


## Draft valuation moved to season projections (2026-08-15)

Implemented the corrected fix. `wes_nfl.season_projections` reads ESPN's kona
endpoint for the entry with `statSourceId=1` (projected) and `statSplitTypeId=0`
(whole season) — deliberately NOT the weekly entries sitting in the same
payload, which are #036's problem.

**The statId mapping was inferred from the data, not copied from a blog post.**
A QB's largest number is passing yards (3), a receiver's is receiving yards (42)
with receptions (53) and targets (58) where a WR/TE has them, and a back's
rushing yards (24) / TDs (25) check out. Volume-only stats (attempts, targets)
are dropped rather than stuffed into `cats`, which by contract holds SCORING
stats.

Because that mapping was inferred, it needed a check that does not lean on the
same inference: score the mapped cats under PPR and compare with ESPN's own
`appliedTotal`.

```
player                    ours(PPR)       espn     diff
Jahmyr Gibbs                  364.5      365.3     -0.8
Puka Nacua                    356.2      356.6     -0.3
Christian McCaffrey           341.9      343.4     -1.5
worst relative gap: 0.5%
```

Consistently ~1 point low, which is the handful of things ESPN's default league
scores and we do not (2PTs, return yards). A silent mapping error would have
mispriced every player with nothing in the output to show it.

**`wes_http.get_json` gained optional headers**, since kona varies its RESPONSE
by an `x-fantasy-filter` header on an otherwise identical URL. The cache key now
includes the headers — keying on URL alone would serve one filter's answer to
another filter's question, a cache hit that is silently the wrong data.

### The board before and after

Same completed mock, same 15 picks, only the valuation changed:

```
BEFORE (2025 actuals)          AFTER (2026 projections)
R5  George Kittle              R5  Terry McLaurin
R6  George Kittle              R6  Courtland Sutton
R7  George Kittle              R7  Courtland Sutton
R8  George Kittle              R8  ...
R12 Tyreek Hill                R12 Michael Pittman
R15 Tyreek Hill                R15 Deebo Samuel
```

It now *progresses* — value declining round by round — instead of repeating one
stale name the market abandoned a hundred picks earlier. Model/engine agreement
moved 87% → 73%, i.e. the model disagrees MORE now, which is at least consistent
with there being something worth disagreeing about.

Kept as a fallback rather than a replacement: `_draft_pool` uses projections and
falls back to actuals, so an ESPN outage degrades the board instead of emptying
it. The two pools stay separate functions on purpose — "what will this player do
this year" and "what has this player been doing" are different questions, and
quietly swapping one for the other is how a board ends up confidently out of date.


## THE DIALOG: a native confirm, auto-dismissed by Playwright (2026-08-15)

Owner: *"you should be able to start it yourself, try again. it's just a dialog
box that pops up."* Correct, and the cause is specific and worth writing down
because it fooled me twice.

**Sleeper confirms with `window.confirm`, and Playwright AUTO-DISMISSES native
dialogs unless a handler is registered.** A native dialog is not in the DOM, so
the symptom is: the click lands, nothing appears anywhere, nothing happens. That
is indistinguishable from "the app ignores synthetic clicks" — which is exactly
what I concluded, twice, having searched the DOM for a modal that could never
have been there.

```
[dialog] confirm: 'Are you sure you want to start the draft?
                   This action cannot be undone
                   Drafting will commence immediately for everyone in your league'
```

`_Session` now registers `page.on("dialog", accept)`. Accepting is right for
this module: nothing here clicks anything the caller has not already decided to
do, every write is behind the kill switch, and the guardrails live above this
layer rather than in a browser prompt.

**Lesson worth keeping:** an element that "does nothing" deserves a check for a
native dialog before it earns a verdict about the platform. The DOM-modal search
was reasonable and found nothing, and finding nothing felt like evidence.

## FIRST REAL PICK SUBMITTED (2026-08-15)

Started a mock, was on the clock at 1.1, and `submit_pick` drafted for real:

```
pick 1  player 9221  Jahmyr Gibbs  slot 1
```

**And it reported failure.** The verification read the picks once, ~3s after the
click, and Sleeper had not committed yet — so a pick that succeeded came back as
*"check the draft room before assuming either way"*.

That is the more dangerous direction of wrong. A false "did it work?" invites a
human into a live draft to fix something that is not broken, and the obvious fix
— pick again — drafts twice.

Two causes, both ours:

1. **One eager read.** Now polled, up to ~15s, bounded.
2. **The read was CACHED.** `draft_picks` holds a 15s TTL, so the verification
   could be served the pre-write answer. **Verifying against a cache is not
   verifying.** Post-write checks now bypass it.

The wait is injectable so the suite does not actually sleep 15 seconds.
