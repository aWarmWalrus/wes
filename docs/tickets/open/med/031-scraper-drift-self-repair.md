---
id: 031
title: Scraper drift — canary detect + escalate-to-Claude repair (not local self-repair)
status: open
priority: med
created: 2026-07-21
closed:
tags: [nba, fantasy, yahoo, espn, scraping, resilience, escalation, llm]
related: ["#029", "#027", "#028", docs/fantasy-gm-design.md]
---

## Problem / Goal
Every external-data path in WES is a scraper against an API/site we don't
control — ESPN's hidden JSON (`pc/wes_nba.py`) and Yahoo's rendered DOM via
Playwright (`pc/wes_yahoo.py`). Both drift without notice: ESPN reshapes a
payload, Yahoo changes a CSS class, and the scrape silently breaks (a wrong
answer, or a graceful-but-useless "couldn't reach"). This is called out as
live risk in #029 §10 (selector drift) and the #027/#029 canary notes, but
there's no defined *repair* flow — today a break just sits until a human
notices and hand-edits selectors.

The tempting idea — "let the local model rewrite the scraper when it breaks" —
was evaluated and **rejected** (see Notes). This ticket documents the flow we
DO want: local model **detects + alerts**, repair **escalates to Claude** (or
a human), local model never owns an unattended scraper-code rewrite.

## Approach
Split by role, matching each tier to what it's actually good at:

1. **Detect (cheap, always-on).** Extend the existing canary pattern
   (`WES_NBA_LIVE=1` / `WES_YAHOO_LIVE=1` schema-drift tests) into a scheduled
   health check (#005 scheduler): periodically run each scraper against live
   endpoints and assert it still *parses* (non-empty, expected fields present,
   not the degradation string). This is pure code + an assertion — no LLM.
2. **Alert (local model's job).** On a canary failure, Jarvis DMs the owner:
   "the ESPN box-score scraper stopped parsing as of <time>." The alert
   watcher + Jarvis-DM plumbing already exists (#029 §8.8 calls this "free,
   high-value"). This is the 12b's appropriate role — noticing and phrasing,
   not fixing.
3. **Repair (escalate — NOT local).** The actual code fix routes up:
   - **ESPN JSON drift** (`wes_nba.py`) is the tractable case — structured,
     small, key-searched already. A Claude-API call given the old parse path +
     an isolated fragment of the new payload can propose the corrected path.
     Possibly a semi-automated "here's the diff, here's a candidate patch for
     review" rather than a blind auto-commit.
   - **Yahoo DOM drift** (`wes_yahoo.py`) is the hard case — CSS selectors
     against large rendered HTML, computer-use-adjacent. Route to Claude
     (API for selector inference on an isolated DOM fragment, or
     `claude-in-chrome`/computer-use to work out the new structure live), or
     just fall back to a human fix. #029 §10 already says this is a "used
     rarely" Claude escalation, not a daily-path capability.
   - **Local 12b's ceiling here** (assessed 2026-07-21, see Notes): can at
     BEST do a narrow, pre-decomposed sub-step (given an isolated fragment +
     the old selector/path, propose a new one) as an assist — never own the
     end-to-end detect→fetch→infer→write→test→commit loop. Not safe for an
     unattended write path (esp. #029's Yahoo write clicks).

## Acceptance
- [ ] Scheduled canary runs per scraper (ESPN + Yahoo) and asserts it still
      parses, not just "returns a string"
- [ ] A parse failure fires a Jarvis alert DM naming which scraper + when
- [ ] Repair path defined + documented: ESPN JSON → Claude-proposed patch for
      review; Yahoo DOM → Claude computer-use / human. Local 12b limited to
      detect+alert (+ optional narrow assist), never autonomous rewrite
- [ ] The two currently-live-only-exercised scrapers get canary coverage —
      note `nba_top_performers` (#028) is only tested live once real games
      exist; fold it in when the season opens

## Notes
### 2026-07-21 — filed after assessing local-model scraper self-repair
Owner asked whether `gemma4:12b` could write code to modify the scrapers when
they break. Assessment: **no, not reliably enough to own it.** Grounds:
- The model's ceiling is documented throughout — under-called tools (#001),
  burned its whole thinking budget emitting zero content (#026 note
  2026-07-21), "weak at computer-use" (#029 §10); Claude is the system's
  error-fallback by design.
- **Context budget** is the hard blocker: WES runs `num_ctx=16384` (~50MB
  KV/1k tok, deliberately capped, design §8.5). A real changed Yahoo/ESPN
  page is tens-to-hundreds of KB of noisy HTML — you can't even feed the
  broken page to a 12b. Isolating the relevant fragment IS the hard part.
- Scraper repair is a **multi-step loop** (detect → fetch new structure →
  infer selectors → write → test → fix); unreliable multi-step planning is
  the still-open #028 gap.
- It'd be an **unattended write path** feeding real Yahoo clicks (#029 rails) —
  "sometimes right" is not a safe bar.

**Nuance kept:** the ESPN JSON side is materially easier than the Yahoo DOM
side (structured, small, already key-searched via `_walk`), so a tightly
prompted 12b could get a *narrow, isolated* JSON-path fix right a fair
fraction of the time — hence the "optional narrow assist" role above. The
Yahoo DOM side is where it fails often. Net design: **12b as the tripwire,
not the mechanic.**
