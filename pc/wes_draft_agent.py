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
import urllib.request

import wes_draft
import wes_schedule
import wes_sleeper

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


def _ask_model(payload, _post_fn=None):
    """One structured call. Returns the parsed dict, or None on any failure —
    the caller falls back to the engine, so this must never raise."""
    body = json.dumps({
        "model": PICK_MODEL, "stream": False, "format": "json",
        "think": False, "options": {"temperature": 0},
        "messages": [{"role": "system", "content": SYSTEM},
                     {"role": "user", "content": json.dumps(payload)}],
    }).encode()
    try:
        if _post_fn is not None:
            raw = _post_fn(body)
        else:
            req = urllib.request.Request(
                OLLAMA_URL + "/api/chat", data=body,
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=90) as r:
                raw = json.load(r)["message"]["content"]
        return json.loads(_strip_fence(raw))
    except Exception:  # noqa: BLE001 — a failed model call must fall back, not raise
        return None


def choose(candidates, context=None, _post_fn=None):
    """Pick ONE candidate. Returns (candidate, reason, source).

    `source` is "model" or "engine", and it is recorded rather than hidden: an
    agent whose judgment silently degrades to a sort is one you cannot evaluate
    later. The ledger should be able to answer "was the model right the twelve
    times it disagreed with the board?"."""
    if not candidates:
        return None, "no candidates", "engine"
    top = candidates[0]

    payload = {"context": context or {}, "shortlist": [
        {"player_key": c.get("player_key"), "name": c.get("name"),
         "position": "/".join(c.get("positions") or []),
         "team": c.get("team"), "value_over_replacement": c.get("vor"),
         "positional_need": c.get("need_bump"),
         "fit_concerns": c.get("fit_reasons") or []}
        for c in candidates]}

    got = _ask_model(payload, _post_fn=_post_fn)
    if not isinstance(got, dict):
        # Deliberately says "no usable reply", not "unavailable": the first
        # version claimed unavailability for what was really an unparseable
        # (fenced) answer, which sent the diagnosis in the wrong direction.
        return top, "no usable reply from the model; took the board's top pick", "engine"

    key = str(got.get("player_key") or "")
    match = next((c for c in candidates
                  if str(c.get("player_key")) == key), None)
    if match is None:
        # THE load-bearing check. A key that is not on the shortlist is a
        # hallucination, a stale board, or a player someone just took — all of
        # which must resolve to the engine's pick, never to a guess.
        return (top, "model chose a player not on the shortlist; took the "
                "board's top pick", "engine")
    reason = str(got.get("reason") or "").strip() or "no reason given"
    return match, reason, "model"


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
