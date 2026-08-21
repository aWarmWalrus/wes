---
id: 041
title: Retrieved qualitative player notes (character, situation, why he is falling)
status: open
priority: medium
created: 2026-08-21
closed:
tags: [fantasy, nfl, notes, retrieval, banter]
related: ["#039", "#040", "#036", docs/data-architecture.md]
---

## Problem / Goal

The owner wants a weekly-refreshed database of the top players with
**qualitative** notes — career trajectory, situation, attitude and character,
what kind of injury a player actually has — "things that won't necessarily show
up in the stats sheet". Two uses: better draft and roster decisions, and better
banter.

**The factual half is already built** (#040, shipped 2026-08-21): injury body
part and notes, news recency, age, experience, depth-chart role, and real
multi-season production for career arcs. All derived from data we already hold,
all checkable against a source in one step, no model involved.

**This ticket is the half that is left**, and it is a different kind of thing:
notes that no structured feed carries. Why a player is falling in drafts. What
his coach has said. Whether a holdout is real. What his reputation is.

## THE PROBLEM WITH THE OBVIOUS IMPLEMENTATION

Point a model at a name and ask for "attitude / character / personality notes",
store the answer, feed it to the banter agent, which posts it into a group chat
under the owner's name.

Every step of that is wrong in a way the rest of this project has been careful
about:

* **A small model will confabulate**, and a confident invented claim about a
  real person does not self-correct the way a bad draft pick does. The board
  gets a chance to prove a pick wrong by Sunday; "he has a reputation for
  quitting on teams" never gets audited by anything.
* **It would be published.** The banter agent posts to real people. This is the
  one output in the whole system that is free text going to third parties, and
  #039 deliberately gave it guardrails about restraint. Feeding it unsourced
  character claims undoes that.
* **It is not falsifiable.** Every other model output here is checkable: a pick
  is one of a verified shortlist, a lineup is a permutation, an injury note
  points at a field in a feed. "Character" claims have no such anchor, which is
  exactly why they are the ones that need one most.

The agent has ALREADY been caught narrating a bye-week check it never performed
(#039, 2026-08-15). That was harmless because the data sat next to it and the
claim was checkable in ten seconds. This would not be.

## What a good version looks like

**Retrieval, not recall.** Every note traces to a document we fetched. The model
summarises retrieved text; it never answers from memory.

**Every note carries provenance.** Source URL, publication date, retrieval date.
A note without one is not stored — the absence of a source is itself the signal
that we should not say it.

**Dated and expiring.** A holdout note from July is wrong in September. Notes
carry a retrieved-at and go stale visibly rather than sitting there looking
current, same rule as the snapshot.

**Scoped to the on-field.** Situation, role, contract, coach's public comments,
injury outlook. Not personality, not character judgements, not anything about
who a person is. The narrower scope is also the more useful one for a fantasy
decision.

**Separated in storage.** Retrieved notes live in their own file with their own
schema and freshness, never merged into the snapshot's factual layer. A caller
should never be unable to tell a fetched fact from a summarised claim.

**The banter agent gets facts, not dossiers.** Even once this exists, what makes
trash talk land is situational — "that's your fourth tight end", "your RB1 is on
PUP with an ACL", "you reached forty spots past his market rank". #040 already
wired the draft state into banter for exactly this reason, and it is the cheaper
and safer route to the same goal.

## Open questions, to settle BEFORE writing code

1. **Source.** Which feeds? A news API, RSS, or the existing `search_web`
   (Haiku web search) path? Rotowire/ESPN player news pages are structured and
   might make retrieval unnecessary for the situational half.
2. **Cost.** ~200-300 players weekly. Cheap on Haiku, not free. Is a weekly
   refresh of the top 200 the right shape, or on-demand for a shortlist?
3. **Scope.** Does the owner actually want character notes, or is "situation and
   why he is falling" the real need? Worth asking directly — the second is
   most of the value at a fraction of the risk.
4. **Sharing.** If a note is only used for OUR decisions the bar is lower than
   if it can be posted to a chat. Should retrieved notes be barred from banter
   entirely?

## Notes

Nothing here is blocking. #040 shipped the factual layer and the draft context
for banter, which is most of the practical value. This ticket exists so the
remaining half is designed rather than improvised, and so the reasons above are
on the record before someone reaches for the obvious implementation.
