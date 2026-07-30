---
id: 033
title: NBA league key is dead after Yahoo's season rollover; fantasy-roster eval will fail
status: open
priority: high
created: 2026-07-29
closed:
tags: [nba, fantasy, yahoo, scraper-drift, eval, config]
related: ["#029", "#031", "#011", docs/fantasy-gm-design.md]
---

## Problem / Goal

**Yahoo has rolled over to the 2026-27 NBA season, and the configured league key
`nba.l.114020` is now dead.** Both the team page and the basketball dashboard
serve a generic marketing page:

```
requested: https://basketball.fantasysports.yahoo.com/nba/114020/1
landed on: https://basketball.fantasysports.yahoo.com/nba/114020/1   (no redirect)
title:     'Fantasy Basketball | Yahoo! Sports'
  table.ysf-rosterswapper  count=0
  table                    count=0
  a.name                   count=0
body: "The 2026 Fantasy Basketball season has not started yet. However, College
       Fantasy Football registration is now open! ..."
dashboard league ids seen: []
```

So `roster("nba.l.114020.t.1")` returns *"That roster came back empty."*

**This regressed between 2026-07-27 and 2026-07-29.** The nightly eval's
`fantasy-roster` golden case passed on 2026-07-27 with a real roster ("15 players
… Tyrese Maxey, James Harden, Scottie Barnes"), so the rollover happened in that
window. Found incidentally while regression-checking the #029 P7 work — **nothing
alerted.**

Confirmed **not** a regression from the sport-parameterization: stashing those
changes and running the original code fails identically.

Two distinct problems fall out:

1. **Stale config.** `~/wes-pc/teams.yaml`'s NBA entry points at a league that no
   longer resolves. It needs a new `league_key`/`team_key` once the owner
   registers a 2026-27 team — and until then there is no NBA team to manage.
2. **The eval will now fail nightly.** `fantasy-roster` asserts against a live
   roster that can't be fetched. It'll show up as a named-case regression in
   `eval_history.csv` tonight, and it is a TRUE failure, not a flake — don't
   silence it by relaxing the case.

## Approach

- **Config:** blank or comment out the NBA team in `teams.yaml` so nothing trusts
  a dead key, and re-add it with the new season's keys when the owner joins a
  2026-27 league. `my_teams()` (now multi-sport) will discover it.
- **Eval:** the `fantasy-roster` golden case should target a team that actually
  exists. Simplest correct fix: point it at the live **NFL** team
  (`nfl.l.957011.t.4`) which has a real drafted roster year-round-ish, rather
  than an NBA team that vanishes every offseason. That also gets the NFL read
  path under nightly regression cover, which it currently lacks.
- **Detection is the real gap** — see #031. A dead league key is exactly the
  "scraper/config drift" class that ticket exists for, and this instance proves
  the need with a concrete 2-day silent window. Cheapest useful canary: assert
  each configured team's roster is non-empty, and alert when one goes empty.

## Acceptance

- [ ] No configured team key points at a league that doesn't resolve.
- [ ] The nightly eval passes without asserting on a league that doesn't exist.
- [ ] A dead/stale league key produces an alert rather than a silent empty roster.
- [ ] `docs/fantasy-gm-design.md` notes that league keys are **per-season** and
      must be re-read at rollover (they are not stable identifiers).

## Notes

- 2026-07-29: the underlying lesson for the epic — **Yahoo league keys are
  season-scoped**, so any long-lived config that names one has a built-in expiry.
  The design treats them as stable identities; they aren't.
- This substantially strengthens the #029 P7 "NFL first" decision. NBA isn't
  merely between seasons — its league is *gone* until the owner re-registers,
  so there is currently no NBA team to automate at all, while the NFL league has
  a real roster today.
- The NFL keys discovered on 2026-07-29 (`nfl.l.424494`, `nfl.l.957011`) will
  have the same expiry after the NFL season ends.
