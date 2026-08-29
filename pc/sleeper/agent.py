"""The draft agent: decide and act on the clock (ticket #039).

WHY THIS IS AN AGENT AND NOT A RANKED LIST
`wes_sleeper.draft_board` is an algorithm — value over replacement, positional
need, roster-fit penalties — and it stops at "here is an ordered list". Real
drafting needs judgment the rules do not hold: positional runs, whether the
board is about to break, a handcuff worth taking early, reaching for a player
whose situation just changed. The owner asked for this to be handled
agentically; a board is an input to that, not a substitute for it.

THE SAFETY PROPERTY, AND HOW IT DIFFERS FROM #038
#038's rule is "the LLM may SUBTRACT, never ADD" — it can veto a roster move but
never originate one, so its worst case is inaction. **Drafting breaks that rule,
because a pick is MANDATORY.** The clock expires, `cpu_autopick` takes the pick,
and "do nothing" is not available. A model that may only veto cannot draft.

So the property is preserved a different way:

    the ENGINE constrains the choice set   ->   the MODEL chooses within it

Every candidate on the shortlist has already been verified available by
player_id, legal under the hard same-team cap, and actually valued. The model
picks one of N; it never names a player freely. That keeps the thing #038
actually protects — **the model's output is a choice among verified options, not
an unverified assertion** — which is the same reason `approve={drop,add}` is
re-checked against the live recommendation rather than trusted.

Anything the model returns that is not on the shortlist is discarded in favour
of the engine's own top pick. A hallucinated name cannot become a pick.

TIMING IS A SAFETY RAIL, NOT A DETAIL
The fallback for failure is `cpu_autopick`, which is *fine* — a CPU pick is far
better than a missed pick. So the agent gives itself a deadline well inside the
clock and simply stands down if it cannot decide in time, rather than racing the
timer and risking a half-submitted pick.
"""
import json
import os
import time
import urllib.request

import wes_draft
from sleeper import draft_log as wes_draft_log
import wes_schedule
from sleeper import data as wes_sleeper

# The judge-style structured-output pattern already used by tests/eval_turns.py:
# a local model, format=json, temperature 0. Same reasoning as there — a small
# model asked for free text will not reliably produce parseable keys.
OLLAMA_URL = os.environ.get("WES_OLLAMA_URL", "http://127.0.0.1:11434")
PICK_MODEL = os.environ.get("WES_DRAFT_MODEL",
                            os.environ.get("WES_ESCALATE_MODEL", "gemma4:12b"))

# How far ahead to start thinking. Deciding at "0 picks away" leaves no room for
# the model call plus the submission; one pick of warning is the difference
# between acting and being autopicked.
PREPARE_WITHIN_PICKS = 1

SYSTEM = (
    "You are drafting a fantasy football team. You will be given a SHORTLIST of "
    "players who are all confirmed available and legal for this roster, each "
    "with a value-over-replacement figure, positional need, and any roster-fit "
    "concerns. Choose exactly ONE.\n"
    "You may disagree with the ordering — that is why you are here rather than "
    "a sort. Consider positional runs, scarcity, roster balance and bye-week "
    "spread. But you may ONLY choose from the shortlist, by player_key.\n"
    "The context gives your CURRENT ROSTER, the starting slots you must fill, "
    "and which of them are still empty. An unfilled starting slot is usually "
    "worth more than another backup at a position you have already stacked: "
    "you can only start so many of one position, and an empty slot scores "
    "zero all season.\n"
    "The context also gives a PHASE. When it is 'starters', filling empty "
    "slots dominates everything else. When it is 'depth', every starting slot "
    "is already filled and value-over-replacement stops being the point — you "
    "are buying insurance and upside, so prefer: a backup to one of YOUR "
    "starters (he inherits the touches if your man is hurt); a young or "
    "newly-promoted player with room to grow over a known low ceiling; and a "
    "player whose bye week you are thin on.\n"
    "MARKET RANK is where the wider market has each player; a low number is a "
    "player others rate highly. Compare it to the pick number: taking someone "
    "far below his rank is a reach, and a player whose rank is far above the "
    "current pick is a bargain that will not last. `picks_until_next_turn` "
    "plus `recent_picks_by_position` tell you what will still be there when "
    "you pick again — if the room has taken four running backs in the last "
    "twelve picks, the running backs you are looking at will not survive. A "
    "market_rank of null means unranked, NOT ranked last.\n"
    "Some entries carry `notes` — injury detail, career arc, depth-chart "
    "role. Those are the ones the board could NOT separate on value, so the "
    "notes are the tiebreak: a rising 24-year-old starter beats a declining "
    "30-year-old backup when the numbers are level.\n"
    "KICKERS AND DEFENCES GO LAST. Do not take one before round 12 unless its "
    "value over replacement towers over everything else on the shortlist. "
    "Replacement level at those positions is nearly flat — the 1st kicker is "
    "barely better than the 12th — so an empty K or DEF slot is the cheapest "
    "hole on the roster to fill, and filling it early costs you a real player. "
    "Their VOR also runs high for a technical reason and is NOT comparable "
    "with a skill player's, so never trade a receiver for a kicker on that "
    "number alone.\n"
    "BYE WEEKS: `bye_counts` in the context is how many players you already "
    "have on each bye week, and each shortlist entry has its own `bye_week`. "
    "Players sharing a bye all sit out the same week together, so adding to a "
    "week you are already stacked on can cost you that week outright. Spread "
    "them where the choice is otherwise close.\n"
    'Reply as JSON: {"player_key": "<key>", "reason": "<one short sentence>"}'
)


def shortlist(league_id, draft_id, roster_id, limit=8, _board_fn=None):
    """The engine's verified candidates, as structured data rather than prose.

    Everything here has already passed availability (by id), the hard same-team
    cap, and valuation. That is what makes it safe to let a model choose freely
    WITHIN it."""
    out = (_board_fn or wes_sleeper.draft_candidates)(
        league_id, draft_id, roster_id, limit=limit)
    if isinstance(out, str):
        return out                     # degradation — relay verbatim
    return out.get("candidates") or []


def _strip_fence(raw):
    """Unwrap a markdown code fence if the model added one.

    Ollama's `format=json` guarantees a bare object; the Anthropic API does not,
    and Claude wraps its reply in ```json ... ```. Without this every Claude
    call parsed as garbage and was reported as "model unavailable" — a
    misleading diagnosis, since the model had answered perfectly well
    (found 2026-08-15 by the replay harness)."""
    t = (raw or "").strip()
    if not t.startswith("```"):
        return t
    t = t.split("```", 2)
    body = t[1] if len(t) > 1 else ""
    if body.startswith("json"):
        body = body[4:]
    return body.strip()


def _ask_model(payload, _post_fn=None, system=None, _kind="draft.pick"):
    """One structured call. Returns the parsed dict, or None on any failure —
    the caller falls back to the engine, so this must never raise.

    LOGGED IN FULL, both sides. This call does not go through `wes_server`, so
    nothing about it reached the turn log or the dashboard: a whole draft left
    behind one `reason` sentence per pick and no record of the shortlist that
    produced it. Working out why a pick looked wrong meant rebuilding the board
    afterwards and guessing. The payload is the question; it is written down."""
    body = json.dumps({
        "model": PICK_MODEL, "stream": False, "format": "json",
        "think": False, "options": {"temperature": 0},
        "messages": [{"role": "system", "content": system or SYSTEM},
                     {"role": "user", "content": json.dumps(payload)}],
    }).encode()
    raw = None
    t0 = time.time()
    try:
        if _post_fn is not None:
            raw = _post_fn(body)
        else:
            req = urllib.request.Request(
                OLLAMA_URL + "/api/chat", data=body,
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=90) as r:
                raw = json.load(r)["message"]["content"]
        got = json.loads(_strip_fence(raw))
        wes_draft_log.log_call(_kind, payload, raw, time.time() - t0,
                               model=PICK_MODEL)
        return got
    except Exception as e:  # noqa: BLE001 — a failed call must fall back, not raise
        # THE FAILURES ARE THE POINT. A model call that returned nothing, or
        # unparseable JSON, is exactly the turn you want to read afterwards --
        # and it is the one a summary-only log throws away.
        wes_draft_log.log_call(_kind, payload, raw, time.time() - t0,
                               error=f"{type(e).__name__}: {e}",
                               model=PICK_MODEL)
        return None


# THE PICK PROMPT AND PAYLOAD ARE FROZEN, AND THAT IS AN EMPIRICAL RESULT.
#
# Adding depth-chart data and a richer reply schema, then measuring on the
# replay harness against one fixed board, gave this — agreement with the
# engine's top pick, and whether the three scarce slots got filled:
#
#   committed baseline ...................  5/15   QB y  K y  DEF y
#   + fields, simple reply ...............  9/15   QB y  K n  DEF n
#   + fields, pruned payload .............  8/15   QB n  K n  DEF y
#   + fields + rich reply ................ 10/15   QB n  K n  DEF n
#   + fields + rich reply, no handcuff
#     paragraph in the prompt ............ 10/15   QB n  K n  DEF n
#
# NOTHING BEAT THE BASELINE, and the damage is not monotonic in prompt size —
# REMOVING text made it worse too. gemma4:12b is at capacity for this task, so
# each perturbation is effectively a coin flip. Rising agreement with the
# engine is the tell: it means the model stopped exercising judgment and went
# back to rubber-stamping the sort, which is the one thing this layer exists
# to avoid.
#
# So the pick call keeps exactly the inputs measured good, and anything we want
# to ADD goes somewhere it cannot move the pick. See `explain`.
def _entry(c):
    """One shortlist row for the PICK call. Frozen — see the note above."""
    return {
        "player_key": c.get("player_key"), "name": c.get("name"),
        "position": "/".join(c.get("positions") or []),
        "team": c.get("team"), "value_over_replacement": c.get("vor"),
        "positional_need": c.get("need_bump"),
        "bye_week": c.get("bye"),
        "market_rank": c.get("market_rank"),
        "injury": c.get("injury"),
        "fit_concerns": c.get("fit_reasons") or [],
    }


def _entry_with_notes(c):
    """`_entry`, plus notes when the engine flagged this row as a close call.

    A row the engine CAN separate keeps the exact frozen payload; only the
    ones it cannot get extra. That is the whole experiment: depth where the
    decision is live, nothing where it is not."""
    row = _entry(c)
    if c.get("notes"):
        row["notes"] = c["notes"]
    return row


EXPLAIN_SYSTEM = (
    "You are reviewing a fantasy football draft pick that has ALREADY BEEN "
    "MADE. It is not yours to change and you must not argue for a different "
    "player — your job is to record accurately what supports it.\n"
    "You get the roster context, the shortlist that was available, and which "
    "player was taken. Some entries carry `handcuff_for`, naming a starter on "
    "this roster that the player backs up: he inherits the touches if that "
    "starter is hurt, which makes him worth more to this team than to any "
    "other.\n"
    "Cite only what the data supports. If a bye week is not crowded, do not "
    "say it was — an invented reason is worse than a short one.\n"
    "Reply as JSON:\n"
    '  "considered": the factors supporting this pick, each a short phrase\n'
    '                with its number, e.g. "value over replacement 7.55, best\n'
    '                available", "fills the empty TE slot", "bye week 6\n'
    '                already has 3 players"\n'
    '  "runner_up":  the player_key of the next-best choice, or null\n'
    '  "why_not":    one sentence on what the runner-up lacks'
)


def explain(candidates, chosen, context=None, _post_fn=None):
    """Describe an ALREADY-MADE pick. Returns (considered, runner_up, why_not).

    A SEPARATE CALL, deliberately. Asking for the rationale in the same breath
    as the decision measurably degraded the decision — five variants, none
    better than the baseline, one that lost the quarterback, the kicker AND the
    defence. Explaining afterwards cannot do that: the pick is fixed before
    this runs, so a verbose or confused answer costs nothing but a dull log
    line.

    It also buys honesty. This model has been caught claiming a bye-week check
    it never performed; a reviewer told the pick is already settled has no
    choice left to justify.

    Best-effort throughout — the caller must treat empty as normal."""
    if not chosen:
        return [], None, ""
    payload = {"context": context or {},
               "shortlist": [dict(_entry(c),
                                  handcuff_for=c.get("handcuff_for"))
                             for c in candidates],
               "player_taken": chosen.get("player_key"),
               "name_taken": chosen.get("name")}
    got = _ask_model(payload, _post_fn=_post_fn, system=EXPLAIN_SYSTEM,
                     _kind="draft.explain")
    if not isinstance(got, dict):
        return [], None, ""
    raw = got.get("considered")
    considered = ([str(x).strip() for x in raw if str(x).strip()]
                  if isinstance(raw, list) else [])
    runner = next((c for c in candidates
                   if str(c.get("player_key")) == str(got.get("runner_up"))
                   and c.get("player_key") != chosen.get("player_key")), None)
    return (considered, runner.get("name") if runner else None,
            str(got.get("why_not") or "").strip())


def choose(candidates, context=None, _post_fn=None):
    """Pick ONE candidate. Returns (candidate, reason, source).

    The compact form, kept because most callers want exactly this. Use
    `decide_one` when you want the factors the model weighed and its runner-up.
    """
    # No explanation call: this form discards it, and a second model round
    # trip nobody reads is pure latency on a clock.
    d = decide_one(candidates, context=context, _post_fn=_post_fn,
                   with_explanation=False)
    return d["candidate"], d["reason"], d["source"]


def decide_one(candidates, context=None, _post_fn=None,
               with_explanation=True, _explain_post_fn=None):
    """Pick ONE candidate, WITH the reasoning behind it.

    Returns a dict: candidate, reason, source, considered, runner_up, why_not.

    `source` is "model" or "engine", and it is recorded rather than hidden: an
    agent whose judgment silently degrades to a sort is one you cannot evaluate
    later. The ledger should be able to answer "was the model right the twelve
    times it disagreed with the board?".

    `considered` and `runner_up` come from a SECOND call, made after the pick
    is already fixed (see `explain`). A one-line reason tells you what the
    model SAID; the factors and the near-miss tell you whether that reason was
    load-bearing — and this agent has been caught narrating a bye-week check it
    never performed (2026-08-15). Asking for both in one call measurably
    damaged the picks, so they are separated.

    Pass `explain=False` on a tight clock: the detail is for the log, and a
    pick is worth more than a paragraph about it."""
    blank = {"candidate": None, "reason": "no candidates", "source": "engine",
             "considered": [], "runner_up": None, "why_not": ""}
    if not candidates:
        return blank
    top = candidates[0]

    def out(cand, reason, source, detail=False):
        rec = {"candidate": cand, "reason": reason, "source": source,
               "considered": [], "runner_up": None, "why_not": ""}
        if detail and with_explanation:
            got = explain(candidates, cand, context=context,
                          _post_fn=_explain_post_fn)
            rec["considered"], rec["runner_up"], rec["why_not"] = got
        return rec

    payload = {"context": context or {},
               "shortlist": [_entry_with_notes(c) for c in candidates]}

    got = _ask_model(payload, _post_fn=_post_fn)
    if not isinstance(got, dict):
        # Deliberately says "no usable reply", not "unavailable": the first
        # version claimed unavailability for what was really an unparseable
        # (fenced) answer, which sent the diagnosis in the wrong direction.
        return out(top, "no usable reply from the model; took the board's "
                   "top pick", "engine", detail=True)

    key = str(got.get("player_key") or "")
    match = next((c for c in candidates
                  if str(c.get("player_key")) == key), None)
    if match is None:
        # THE load-bearing check. A key that is not on the shortlist is a
        # hallucination, a stale board, or a player someone just took — all of
        # which must resolve to the engine's pick, never to a guess.
        return out(top, "model chose a player not on the shortlist; took the "
                   "board's top pick", "engine", detail=True)
    reason = str(got.get("reason") or "").strip() or "no reason given"
    return out(match, reason, "model", detail=True)


def format_decision(d):
    """The decision as log lines. Multi-line on purpose: a draft is reviewed
    afterwards, and a rationale squeezed onto one line is a rationale nobody
    reads."""
    if not d.get("candidate"):
        return d.get("reason", "no decision")
    lines = [f"{d['candidate']['name']} ({d['source']}: {d['reason']})"]
    for f in d.get("considered") or []:
        lines.append(f"    weighed: {f}")
    if d.get("runner_up"):
        why = d.get("why_not") or "no reason given"
        lines.append(f"    over: {d['runner_up']} — {why}")
    return "\n".join(lines)


def decide(league_id, draft_id, roster_id, limit=8, _board_fn=None,
           _post_fn=None):
    """Full decision for the current clock: shortlist -> choice -> explanation.

    Degrades to a string on any problem; never raises into a turn."""
    cands = shortlist(league_id, draft_id, roster_id, limit=limit,
                      _board_fn=_board_fn)
    if isinstance(cands, str):
        return cands
    if not cands:
        return "No draftable candidate could be valued right now."

    pick, reason, source = choose(cands, _post_fn=_post_fn)
    pos = "/".join(pick.get("positions") or []) or "?"
    note = "" if source == "model" else "  (engine fallback)"
    return (f"Take {pick['name']} ({pos}, {pick.get('team')}) — {reason}"
            f"{note}")
