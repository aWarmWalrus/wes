---
id: 040
title: Qualitative player notes from data we already hold
status: shipped
priority: medium
created: 2026-08-21
closed: 2026-08-21
tags: [fantasy, nfl, notes, snapshot]
related: ["#039", "#041", "#036"]
---

## Problem / Goal

The owner wanted a weekly-refreshed database of the top players with notes that
a stat line does not carry — career trajectory, what kind of injury a player
actually has, situational colour — for better decisions and better banter.

Recon showed **most of the factual half was already being paid for and thrown
away.** Sleeper's 14MB daily dump carries, for the 648 players with an injury
status:

| field | coverage |
|---|---|
| `injury_body_part` | 570/648 — "Knee - ACL", "Achilles" |
| `injury_notes` | 84/648 — "Surgery" |
| `news_updated` | 580/648 |
| `age` / `years_exp` / `college` | mostly present |
| `depth_chart_order` | 1,843 players overall |
| `injury_start_date`, `practice_participation` | 0/648 — never populated |

`parse_players` was reducing all of it to the single word `PUP`. "PUP" and
"PUP — Knee - ACL (Surgery)" are different facts, and the difference decides
whether you draft him.

## What shipped

**`pc/wes_notes.py`** — PURE (no network, no clock; `now` is passed in) and
turns held data into checkable one-liners:

```
injury      PUP — Achilles (Surgery); last news 5 days ago
severity    cannot practise yet
trajectory  25, 4th year — 28.4 -> 10.2 -> 21.9 pts/g, volatile, trending up
role        RB2 behind Saquon Barkley
```

**Career arcs are real production**, not a guess: `wes_snapshot` now carries
three prior seasons of per-game points by `espn_id` (`HISTORY_SEASONS`), built
in ~11s at snapshot time and never on a clock. 218 players, 72 with a
multi-season arc.

**Notes go to the EXPLANATION call and to banter — never to the pick payload.**
That payload is frozen on measured evidence (#039): every field added to it made
the local model measurably worse. The explanation call runs after the pick is
already fixed, so it cannot damage a decision.

**Banter gained the draft state**: recent picks, every team's roster shape, our
own injuries in words. It previously had `{round, picks_made,
picks_until_our_turn}` and nothing about who drafted what, so it could only
produce generic ribbing.

## Two bugs the real data caught

**Direction from the wrong end.** Comparing first-against-last called Puka Nacua
"declining" on `28.4 -> 10.2 -> 21.9` — arithmetically true and useless, since
what a manager wants to know is that he bounced back. Direction now comes from
the most recent move, and a big swing is called `volatile` out loud rather than
averaged away.

**A stray space before a semicolon**, from `" ".join`. Trivial, but it is the
kind of thing that only shows up when you print real output instead of trusting
the unit test.

## Deliberately NOT done

Character, attitude and "why he is falling" notes need retrieval with sources —
that is **#041**, design-only on purpose. The reasoning is on that ticket.

## Notes

`practice_participation` and `injury_start_date` are empty in the offseason. If
they populate in-season they are the two most useful additions here, and both
are free.
