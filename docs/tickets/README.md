# WES tickets

Task tracker for WES — one markdown file per ticket, git-versioned with the
code. Replaces the old monolithic `docs/roadmap.md` (migrated 2026-07-06) so
completed work stops costing context: only **open** tickets are indexed and
loaded; `done/` is an archive you read on demand.

## Layout

```
docs/tickets/
  README.md              # this file — conventions
  INDEX.md               # OPEN tickets only, one line each (the cheap-to-load view)
  open/
    high/  NNN-slug.md   # priority tiers
    med/
    low/
  done/
    YYYY-MM/ NNN-slug.md # closed tickets, filed by close month
```

- **Filename**: `NNN-slug.md` — `NNN` is a zero-padded, never-reused id (a
  stable handle, not a priority; ids are assigned in creation order).
- Priority lives in **both** the folder (`open/high|med|low`) and the
  frontmatter, so it's visible whichever way you're looking.

## Ticket format

```markdown
---
id: 001
title: Short imperative title
status: open            # open | done
priority: high          # high | med | low
created: 2026-07-06
closed:                 # set to a date when status: done
tags: [router, vision, discord]
related: [<commit sha>, docs/data-architecture.md, "#002"]
---

## Problem / Goal
What and why. For bugs: the symptom + evidence (a /turns row, a log line).

## Approach
Fix direction / design. Concrete enough to start from cold.

## Acceptance
- [ ] checkable outcomes

## Notes
Running log — findings, decisions, dead ends. Append, don't rewrite.
```

## Workflow

- **Open a ticket**: create `open/<priority>/NNN-slug.md`; add one line to
  `INDEX.md`. Bump the id past the current max (check INDEX + `done/`).
- **Close it**: set `status: done` + `closed:` date, move the file to
  `done/YYYY-MM/`, and delete its line from `INDEX.md`. The body (context,
  what shipped, commit refs) is the permanent record.
- **Reprioritize**: move between `open/high|med|low/`, update its `INDEX.md`
  line. **Split/merge**: cross-link with `related: ["#00N"]`.

## For the agent (important)

Read **`INDEX.md`** to see current work — it's the only part meant to be loaded
by default. Open an individual ticket file for full context when you act on it.
**Don't load `done/`** unless you specifically need the history of a shipped
feature (git log + `docs/` usually suffice). When you finish work, close the
ticket in the same change as the code, like tests and docs.
