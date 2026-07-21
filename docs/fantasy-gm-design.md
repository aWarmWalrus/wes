# Fantasy GM — autonomous fantasy-sports team management

> **Status:** design / roadmap (2026-07-16). Tracked by ticket **#029** (the
> umbrella epic); individual phases are spun out as their own tickets when work
> starts. This doc is the durable design; the ticket is the live status.

Give Jarvis the ability to **run a fantasy team like a GM** — read the league,
value players against *your league's* scoring, decide the moves (lineup, waivers,
trades), and — gated by a per-team autonomy setting — actually execute them on
the platform, reporting back over Discord/voice.

First target: **NBA**, on **Yahoo** — but driven through the **web UI with
scripted browser automation**, not the official API (see the 2026-07-17 decision
record below). Built to grow into a **multi-team portfolio** where each team has
its own autonomy level.

> **DECISION (2026-07-17): the official Yahoo Fantasy API is OFF the table.**
> Yahoo now gates *all* API access — read included — behind a manual application +
> DocuSign that took a community dev **3 weeks** and granted **read only** (write
> still pending). Worse, the agreement's terms **prohibit storing/caching Yahoo's
> data**, requiring deletion within 30 days. That is fundamentally incompatible
> with this system's caching-first design (§8.3/§8.7: the nightly all-team cache,
> precomputed rosters, sqlite fact store) and with an autonomous *write* agent.
> Applying at all — for a bot that writes moves and caches league state — would
> likely put us offside the agreement. **We keep Yahoo as the platform** (that's
> where the owner's league is) **but reach it the same way a person does: a
> logged-in browser session driven by scripted Playwright.** This changes only
> the *ingestion + execution adapter*; the engine (§2), rails (§5), autonomy
> config (§4), and optimizer (§8.2) are untouched. See §7-P0/P3 and §10.

---

## 1. Scope & the three decisions

| Decision | Choice (2026-07-16) | Why it matters |
|---|---|---|
| **Sport** | NBA first | Reuses `wes_nba.py` (ESPN client) + r/GoNets infra; Nets already the default team. Engine designed sport-agnostic so NFL/MLB can plug in later (P7). |
| **Platform** | **Yahoo (via browser automation)** | Owner's league lives on Yahoo. The *official API* was ruled out 2026-07-17 (access now needs a manual approval + a no-caching DocuSign — see the decision record above), so we drive the **web UI** with scripted Playwright instead. The write mechanism is now UI-agnostic, so ESPN/Sleeper are no longer disqualified for lacking a write API — but Yahoo stays because that's where the league is. |
| **Autonomy** | **Per-team, configurable** | Jarvis may run several teams at once; each team is independently `advise`, `propose`, or `auto`. This is a *config model*, not a global switch — see §4. |

**Offseason note:** it's July — NBA season tips in October. That's the *right*
window to build read/valuation/optimizer plumbing and shadow-test it, so writes
are battle-tested before games matter.

---

## 2. What a GM actually has to do

The engine breaks into ingestion → valuation → decision → execution, wrapped in
config, scheduling, and rails.

1. **League-state ingestion** (per team) — scoring format (H2H-categories /
   H2H-points / roto), roster slots + position eligibility, current roster, this
   week's matchup, standings, waiver/FAAB rules and budget, transaction limits,
   lineup lock times.
2. **Player valuation** — full stat lines (not just points), rolling averages,
   role/minutes trends, injury & availability status, **schedule density**
   (games this week, back-to-backs), and projections — all mapped through *this
   league's* scoring so "value" means value *to you*.
3. **Decision engines** (increasing difficulty):
   - **Daily lineup optimizer** — today's games + injuries + slots → the active
     lineup that maximizes expected production. Highest-frequency, highest-value,
     **lowest-risk** automation. This is the anchor feature.
   - **Waiver / FA engine** — add/drop candidates, streaming for schedule, FAAB
     bid sizing vs budget.
   - **Trade engine** — grade incoming offers, propose trades, balance categories.
   - **Matchup strategy** — in H2H-category leagues, punt strategy and which
     categories to chase given the week's opponent.
4. **Execution layer** — write moves to Yahoo, through **one gated executor**
   that enforces the team's autonomy mode + guardrails and logs every action.
5. **Config & rails** — per-account OAuth, per-team autonomy, guardrails
   (never-drop lists, FAAB caps, move-volume caps, dry-run), action ledger, undo
   window, idempotency. See §4–§5.
6. **Scheduling & monitoring** — daily pre-lock optimizer runs, waiver-run-day
   claims, injury/news watch before lock (reuse the existing alert watcher +
   r/GoNets). See §6.
7. **Surfaces** — Discord for proposals/approvals/after-action reports (it's
   already the owner-DM channel with per-channel memory); voice for quick asks
   ("who should I start tonight?").

---

## 3. How it maps onto WES (reuse before build)

This project is largely **#027 taken to its logical end + write access**, riding
capabilities other tickets already scope:

| Existing | Role in Fantasy GM |
|---|---|
| **#027** NBA data (`wes_nba.py`, ESPN free API) | Foundation. Extend from points-only to full stat lines + projections (P1). |
| **#028** Planner / multi-step | The GM decision loop **is** a multi-step planner (roster → projections → optimize → act). Fantasy GM is a prime consumer; may drive #028's option B/C. |
| **#026** Adaptive thinking budget | GM decisions route to the deep tier (12b + thinking) or Claude escalation; "what's my roster" answers on a plain 12b pass. |
| **#005** Scheduled actions | The scheduler for daily lineup locks + waiver runs. Fantasy GM is its first serious user. |
| **#012** Durable memory | Store league settings, strategy prefs, never-drop lists, per-team notes. |
| **#004** Smart-home actions | Sibling "Jarvis takes real-world gated actions" pattern — **share the confirm/rails/executor design** rather than inventing two. |
| **#002** Escalation announced-not-executed | The follow-through gap. Moves that get *promised* but never *called* is exactly the failure we cannot ship with — prerequisite class of fix. |
| MCP client layer (#027 P3) | Optional. Yahoo is a plain OAuth REST API; wrap directly like ESPN unless MCP reuse elsewhere justifies it. |

New components this project adds: `pc/wes_yahoo.py` — **now a Playwright
browser-automation adapter** (logged-in session → scrape read state, script the
write clicks), *not* the retired OAuth client — the valuation/optimizer engine,
the **gated executor + action ledger**, and the **per-team config** (§4).

---

## 4. Autonomy & config model (the portfolio idea)

Jarvis manages **N teams across M accounts**; each team is configured
independently. Config is declarative; **secrets never live in the repo** (Yahoo
OAuth tokens go in the PC user env / a token store, per the existing secrets
rule).

Sketch (`nba/fantasy/teams.yaml` on the PC, alongside the token store):

```yaml
accounts:
  main:   { yahoo_token_ref: YAHOO_TOKEN_MAIN }   # value in PC user env
  altleague: { yahoo_token_ref: YAHOO_TOKEN_ALT }

teams:
  - name: "Dinosaurs"
    account: main
    sport: nba
    league_id: "466.l.12345"
    team_id: "466.l.12345.t.3"
    autonomy: auto            # advise | propose | auto
    guardrails:
      max_faab_bid: 25        # % of budget per claim
      max_moves_per_week: 4
      never_drop: ["Victor Wembanyama"]
      protected_slots: []
      actions_allowed: [set_lineup, waiver_claim]   # e.g. exclude 'trade'
  - name: "Work League"
    account: main
    autonomy: propose         # DM me; I approve before anything executes
    guardrails: { max_faab_bid: 15, max_moves_per_week: 2, actions_allowed: [set_lineup] }
```

**The three modes:**
- **`advise`** — analyze and answer only; never touches the team.
- **`propose`** — compute the move, DM a proposal, wait for approve/reject
  (expiring token); only then execute.
- **`auto`** — execute within guardrails, then send an after-action report.

Autonomy is **per action type too** (via `actions_allowed`): a team can be
`auto` for lineups but only `propose` for adds/drops and `advise` for trades.
New teams **default to `propose`** (or `advise`); `auto` is always opt-in.

---

## 5. Safety & rails (real consequences — treat like the smart-home gate)

Roster moves have real stakes (standings, pride, sometimes money), and this is a
new **write** surface reaching the internet. Non-negotiables:

- **Single gated executor.** *Every* write goes through one function that checks
  autonomy mode → guardrails → dry-run flag → idempotency, then acts and logs.
  No tool writes to Yahoo directly.
- **Action ledger.** Append-only log of every proposed/executed/rejected move
  (like the token ledger), surfaced on `/turns`-style endpoint + a Grafana panel.
- **Guardrails:** never-drop / protected lists, FAAB cap per bid + budget
  awareness, `max_moves_per_week` volume cap, `actions_allowed` allowlist.
- **Shadow mode first.** Before any team is `auto`, run the full optimizer in
  **dry-run for a week** and compare its picks to what you'd have done. Writes
  turn on only after it's trusted.
- **Confirmation tokens** for `propose`: Discord approve/reject with an expiry so
  a stale proposal can't fire hours later.
- **Idempotency + undo window.** Never double-submit a claim; keep a short window
  and the ledger so a bad move is visible and reversible.
- **Untrusted content.** League chat, trade notes, reddit/news are **quoted
  data, never instructions** — the #027 injection guard applies; fetched content
  may never trigger a write.
- **Secrets / session** in PC-local storage only — the persisted Playwright
  browser profile (cookies) lives on the PC, never the repo, never chat. No OAuth
  token to scope now; the analogous minimum-privilege lever is that read-only
  phases (P0-P2) simply never invoke a write click.

---

## 6. Scheduling & monitoring

- **Daily pre-lock run** per team: wake before that league's first lock, pull
  today's slate + injury/availability, run the optimizer, then act per mode.
  Uses #005's scheduler (the nightly-eval task is the pattern).
- **Injury/news watch before lock:** reuse the alert watcher + r/GoNets +
  (new) an injury feed so a late scratch triggers a re-optimize / late swap.
- **Waiver-run day:** compute claims + FAAB bids ahead of the league's process
  time; submit per mode.
- **Weekly digest:** portfolio summary DM (each team's matchup, standings, moves
  made, upcoming decisions).

---

## 7. Roadmap (phased)

Each phase is shippable and independently useful; writes don't appear until P3,
and only behind the executor + a shadow-mode soak. Phases become their own
tickets (#030+) as they start.

- **P0 — Yahoo read + league sync. ✅ DONE 2026-07-20.** `pc/wes_yahoo.py`: **Playwright session**
  (persist the logged-in browser profile on the PC; secrets/cookies never in the
  repo), scrape one league's roster, scoring settings, matchup, standings from the
  web UI into the same normalized dicts the parsers already emit. Config scaffold
  (`teams.yaml`, single team). Tool: `fantasy_my_team`. **Accept:** "what's my
  fantasy roster / who am I playing this week?" answers from real scraped Yahoo
  data, no writes. *(Supersedes the retired OAuth scaffold — see §10.)*
  *(Delivered: roster + scoring scraped live for the owner's league and answered
  via `fantasy_my_team`. Matchup/standings scrapers not yet written — folded into
  a later phase; roster+scoring is the shipped P0 gate.)*
- **P1 — Valuation + full stat lines. ✅ DONE 2026-07-20.** Extend `wes_nba.py` to full box-score
  stat lines + rolling averages + injury/availability; add a projection source
  mapped to the league's scoring. Tool: `fantasy_player_value`.
  **Accept:** "is X worth starting over Y in my league?" grounded in stats +
  *this* league's scoring, no guesses.
  *(Delivered: `wes_nba` season-stats layer (name→ESPN-id search + latest-season
  parse) + new `pc/wes_fantasy.py` engine; `fantasy_player_value` maps a real
  ESPN stat line to the league's roto categories, with an optional head-to-head
  compare. Scope: category line, not a z-score ranking — z-scores need a
  population fetch, deferred to the optimizer. Rolling averages / a dedicated
  projection source also deferred; latest-season averages are the P1 baseline.)*
- **P2 — Daily lineup optimizer (advise / dry-run). ~ ENGINE DONE 2026-07-21.** The engine: today's games
  + injuries + slot eligibility → optimal active lineup, explained; recommendation
  only. **The optimizer is deterministic code (ILP/Hungarian), not an LLM** — the
  model only explains the result and handles edge-case judgment (§8.1-8.2), so
  **#028 is not a blocker**. **Accept:** pre-lock, "set my optimal lineup" returns
  a correct, explained lineup with zero writes. Soak in shadow mode.
  *(Delivered: `wes_fantasy.optimize_lineup` — exact capacity-DP assignment,
  dependency-free, property-tested vs brute force — + `roto_scalar` interim value
  + `fantasy_optimize_lineup` assembly (advise/dry-run, degrades offseason). Tool
  registration + golden case + shadow soak DEFERRED to in-season, when Yahoo shows
  eligible positions and games exist. Real z-scores still deferred; the interim
  spread-normalized scalar orders players for now.)*
- **P3 — Executor + autonomy config + rails.** The gated executor (§5), Yahoo
  `set_lineup` write **via scripted Playwright clicks** (the optimizer decides the
  move; the script replays it — the LLM never free-drives the page, §10), per-team
  modes (§4), guardrails, action ledger, Discord approve/reject for `propose`,
  after-action reports. Wire P2 to actually set the lineup under each team's mode.
  **`propose`-mode writes come first**; browser `auto`-mode is held until the write
  script is proven in shadow (§10). **Accept:** `propose` DMs a proposal → approve
  → lineup set on Yahoo + logged; `auto` sets it and notifies; guardrails block a
  disallowed move.
- **P4 — Scheduling + monitoring.** Per-team pre-lock scheduled run, injury/news
  watch + late swap (§6). **Accept:** lineups are managed daily per each team's
  mode with no manual trigger.
- **P5 — Waiver / FAAB engine.** FA evaluation vs roster, schedule streaming,
  FAAB bid sizing, add/drop proposals + execution under mode/guardrails,
  waiver-day scheduling. **Accept:** weekly waiver targets proposed/claimed per
  mode within budget + volume caps.
- **P6 — Trades + matchup strategy.** Grade incoming offers, propose trades,
  H2H-category punt strategy per week. Highest reasoning bar → Claude escalation
  likely. **Accept:** an incoming trade gets a graded, category-aware
  recommendation; weekly strategy accounts for the matchup.
- **P7 (stretch) — Portfolio + generalization.** Manage N teams across accounts
  from config; sport-agnostic adapter seams for NFL/MLB; weekly portfolio digest.

**Critical path to the headline feature** (Jarvis auto-sets my lineup): P0 → P1 →
P2 → P3 → P4. P5/P6 extend GM scope; P7 scales it out.

---

## 8. Execution & context architecture (how the loop actually runs)

Designed 2026-07-16 against the **measured** hardware budget, not estimates.

### 8.1 The headline: this is a pipeline, not an agentic loop
The daily GM run is a **known workflow**, so nothing needs to plan it:

```
sync league -> update stats -> value players -> optimize lineup -> execute -> report
```

That's a **fixed DAG of deterministic code with bounded LLM calls at judgment
nodes only**. Agentic loops are for problems whose *shape* is unknown; the daily
run's shape is known and fixed. Open-ended ad-hoc questions ("should I trade X
for Y?") are the other mode — that's #028's planner, and it's *fast* precisely
because the batch phase already did the work.

> **Consequence: #028 is NOT on the critical path.** P0-P4 ship without it. This
> supersedes §7's earlier note that P2 "runs on the deep model / planner".

### 8.2 The LLM is the wrong tool for most GM work
- **Lineup optimization is an assignment problem** — solve it *exactly* in Python
  (ILP / Hungarian), never by asking a 12B to eyeball a table.
- **Valuation is arithmetic** (z-scores against league scoring). **Projections are
  statistics.** Both are code.
- The model's real job: **judgment at the margins** (is this streamer worth the
  roster churn?), ambiguity, and **explanation in English**.

This is simultaneously the biggest context win and the biggest *correctness* win.

### 8.3 The context rule: the model never sees a raw API payload
A 13-player roster as raw Yahoo/ESPN JSON is ~20-40k tokens. The same roster as a
precomputed table — one line per player (name, pos, status, value, tonight's
game, trend) — is **~400 tokens**. Python fetches and computes; the model reads
the table.

> **If a GM call ever needs >16k context, that's a bug in the precompute — not a
> reason to raise `num_ctx`.**

### 8.4 Fan out with isolated contexts; never accumulate
Twelve waiver candidates = twelve **independent** ~1-2k-token calls (this player +
roster summary -> score + one-line rationale), then **one** synthesis call over
twelve one-liners. Contexts never merge. A ReAct loop would pile all twelve into
30k+ and reason *worse*. Small units are also **interruptible**, which is what
keeps a Discord DM from queueing behind a long generation (§8.6).

### 8.5 Measured VRAM / context budget (2026-07-16)
Single-model topology: **gemma4:12b** does router + escalation + VLM. (`gemma3:4b`
has vision but **no `tools`** — it can never be the router.)

| Quantity | Measured |
|---|---|
| Total VRAM | 15.9 GB |
| Desktop baseline + CUDA context | ~2.0 GB |
| **Usable** | **~14.3 GB** |
| gemma4:12b weights | 7.0 GB |
| Resident @ `num_ctx=16384` | **7.8 GB** -> KV ~0.8 GB |
| **KV cost rate** | **~50 MB per 1k tokens** |
| **Unspent headroom** | **~6.5 GB** |

Extrapolated KV: 32k ~1.6GB, 64k ~3.2GB, 128k ~6.4GB. The model advertises
262144 max context = **~12.8GB of KV alone** — a trap. Overflow spills to CPU,
which is a cliff, not a slope.

**Headroom does not buy intelligence.** Weights determine quality; idle VRAM
changes neither reasoning nor tok/s. Its only real uses are (a) a *bigger model*
(the sole genuine intelligence lever — ~13GB of weights would fit, roughly a 2x
param jump; settle it empirically with `models fit` + `eval_turns.py`), or (b) a
modest ctx bump. **Deliberately unspent for now (owner, 2026-07-16): stay on 12b.**

### 8.6 Model swapping: scheduled yes, reactive no
Never swap *per-request* — a DM landing mid-batch would eat a **64s** cold-load
(measured warmup). But a **scheduled** swap is fine: load a big analysis model at
09:00, run for hours, unload, restore 12b before the owner is home. Amortized
over hours, not requests. Machinery already exists:
`wes-dev.ps1 models load|unload` as the batch job's bookends.

Batch work must **yield** to interactive: the owner is away but Discord *is* the
remote frontend. Small units (§8.4) keep the Ollama queue draining in seconds.

### 8.7 State lives in sqlite, not in the context window
Stat history, projections, and the action ledger are a **database the model
queries via tool**. Draw a hard line against #012:

- **MEMORY.md** -> preferences/strategy ("I punt FT%").
- **sqlite** -> facts (stat lines, ledger, projections).

Stat tables must never leak into MEMORY.md.

**On RAG:** the core GM data is *structured* — that's `SELECT`, not vector
search. RAG earns its place only for *unstructured* prose (news, injury reports,
r/GoNets). Note RAG is a technique for **avoiding** big context, not a consumer
of it; its VRAM cost is a ~300MB embedding model, so headroom was never its
constraint.

### 8.8 Unattended robustness (nobody is watching)
- **Checkpoint every stage; idempotent; resume, don't restart.**
- **Fail-safe = don't act.** Bad/stale data -> alert + degrade to `propose`,
  never set a garbage lineup. Nuance: a stale lineup with an injured starter also
  loses, so the rule is *act only on high confidence*, not *never act*.
- **Deadline-aware degradation:** if the pre-lock run is late, drop thinking and
  take the fast path rather than miss the lock. Degrade the model, not the
  deadline.
- **Alert on non-completion.** Prometheus + the alert watcher + Jarvis DMs already
  exist; *"the GM run didn't complete today"* is a free, high-value rule.
- This is where **#002** bites hardest: unattended, a promise that never executes
  is completely silent.

### 8.9 The day's shape
Locks are **per-game** in most Yahoo NBA daily leagues — compute from the
schedule, never hardcode.

| When | Work | Profile |
|---|---|---|
| ~09:00 | Ingest last night's box scores, update rolling averages/projections, rescan + rank FA pool | GPU-heavy, latency-tolerant, owner away |
| ~15:00-16:00 | Injury/news sweep, provisional lineup, **DM proposals** (owner can approve from work) | light |
| T-45min from first tip | Final scratch check, execute per mode | fast path, deadline-bound |
| per-game | Late swap as later games approach | light |
| overnight | Ingest finals, ledger, weekly digest | batch |

## 9. Cross-cutting

- **Testing** (per `tests/README.md` + [[wes-testing-rigor]]): deterministic unit
  tests for valuation + optimizer (fixed stat fixtures → known optimal lineup);
  a `WES_YAHOO_LIVE=1` schema-drift canary like the NBA one; dry-run e2e against a
  real read before any write test. Add a golden eval case per new tool.
- **Observability:** action ledger endpoint + a Grafana "Fantasy moves" panel;
  reuse the token/turn-log patterns.
- **Security:** OAuth secrets in PC user env only; minimum token scope; injection
  guard on all fetched/league content; the gated executor is the only write path.
- **Open questions to resolve as we build:** projection source (scrape ESPN
  projections vs compute from rolling averages vs a paid feed); whether to model
  H2H-category punt strategy in P2 or defer to P6; browser-automation specifics
  (headless-vs-headed for login/2FA, how to keep the Yahoo session alive
  unattended, selector-drift monitoring — see §10).

---

## 10. Reaching Yahoo without the API (2026-07-17)

The official API is out (see the §1 decision record). We drive the Yahoo **web
UI** as the owner would, in their own logged-in session. This is a *personal,
single-account* automation — the owner doing by script what they'd do by hand,
with no data redistribution — a very different posture from a distributed app
storing other users' data. It may still be contrary to Yahoo's site ToS; that's
an accepted, owner-approved risk for personal use, kept single-account and
low-volume.

**Principle (mirrors §8.1-8.2): deterministic script, not an LLM driving the page.**
- **Read + write via scripted Playwright** — fixed selectors, deterministic
  clicks, against a persisted logged-in profile. Fast and inspectable.
- **The LLM chooses the *move*, never the clicks.** Valuation + optimizer decide
  *what*; the browser script executes it. This is what keeps every rail in §5
  intact: an action is validated, guardrail-checked, and confirmation-tokened
  **before** any click — impossible if a model free-drives the page.
- **Vision / LLM-actuation is fallback only** — for when a selector breaks, not
  the daily path. (Local Gemma is weak at computer-use anyway; this would be a
  Claude computer-use / `claude-in-chrome` escalation, used rarely.)

**Consequences vs the API design:**
- The no-caching clause no longer binds us — we're not API consumers — so the
  caching-first architecture (§8.3/§8.7, #027 P2 all-team cache) stands.
- **`wes_yahoo.py` is re-scoped** from OAuth client to Playwright adapter; its job
  is still to emit the same normalized roster/scoring dicts, so the parsers,
  formatters, and everything downstream are reused. The OAuth-specific scaffold
  (token lifecycle, `authorize_url`, `yahoo_connect.py`, the fspt-scope tests) is
  **retired** — tracked for removal in #029.
- **New fragility to manage:** selector drift (a Yahoo layout change silently
  breaks reads/writes → a canary that asserts the scrape still parses, like the
  old schema-drift canary), session expiry (re-login flow, possibly 2FA), and
  bot-detection (keep it headed/low-volume/human-paced).
- **Shadow mode gets more important, not less:** the write script is now the
  riskiest component, so P3 soaks browser writes in dry-run (compute + log the
  clicks without committing) before any `auto` team is enabled.
```