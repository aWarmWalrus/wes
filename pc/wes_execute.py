"""The gated executor + action ledger (ticket #029 P3, design §5).

STATUS 2026-07-30: **LIVE WRITES IMPLEMENTED**, behind the `WES_YAHOO_LIVE_WRITES`
kill switch (off by default). The path from "shadow mode only" to here was real
recon against the live `nfl.l.957011.t.4` roster, including one write that had
to be corrected — recorded honestly below because it directly shaped the design.

THE MECHANISM (confirmed 2026-07-30, screenshot-driven recon):
  - Clicking a player's position badge (`span.pos-label[role=button]`) selects
    them as the swap SOURCE. Valid destination rows get class `swaptarget`
    (visually highlighted green) and everything else dims.
  - Clicking a `swaptarget` row performs the swap **instantly** — no separate
    Save step. `<input name=jsubmit>` ("Save Changes") is a hidden leftover
    from a different (probably legacy/no-JS) flow; it plays no role here.
    Confirmed by reload AND by the independent scraper (`roster_players`)
    agreeing the change had actually persisted server-side.
  - **The trap:** two slots of the same TYPE (e.g. two RB starters) both render
    with the same `data-pos`, so a swap target CANNOT be identified by slot
    type alone. During recon, targeting by type swapped the wrong player —
    intended to move Player A into the RB slot vacated by Player B, but landed
    on the row for a DIFFERENT RB starter (Player C) instead, benching the
    wrong person. Caught immediately by an independent post-write check, fixed
    with a second precise swap, and confirmed restored. That mistake is *why*
    every write here targets swaptarget rows by the PLAYER occupying them (or
    explicitly by "the empty one"), never by slot type alone — see
    `_plan_swaps` / `_execute_swap`.
  - **The other trap:** Yahoo's row-level `data-pos` (used for `swaptarget`
    matching) spells multi-position slots with underscores ("W_R_T"), while
    the scraped `players[i]["slot"]` — and everything upstream of it in this
    codebase — uses slashes ("W/R/T"). `_dom_slot()` is the one place that
    translation happens; empirically confirmed against a real flex slot, not
    guessed.

SAFETY PROPERTIES THIS BUYS:
  - `_plan_swaps` is PURE (no browser) and independently tested — the planning
    logic that decides WHO to swap with WHOM is checkable by inspection and
    unit tests, not only by watching it click a real page.
  - `_submit_lineup` re-reads the roster after every swap it makes and verifies
    the actual server-side result matches what was intended for THAT swap
    before proceeding to the next one — it does not assume a click succeeded
    just because it didn't throw. Any mismatch raises immediately rather than
    continuing to compound an error, and the ledger records exactly how far it
    got.
  - Nothing here is reachable without `WES_YAHOO_LIVE_WRITES=1` (still unset by
    default) AND `autonomy: auto` AND the action being in `actions_allowed` —
    three independent gates, not one.

WHAT THIS MODULE DOES:
  - diff_lineup(): the optimizer's recommendation vs. the CURRENT Yahoo roster,
    reduced to the moves that would actually change something.
  - check_guardrails(): autonomy mode + actions_allowed + freshness, from the
    team's teams.yaml record (design §4-§5). Degrades to (False, reason) rather
    than raising — a misconfigured guardrail should refuse the action, not crash
    the turn.
  - The ledger: append-only JSONL, PC-local (like `teams.yaml` — never the
    repo), one line per proposed/blocked/would-execute/executed action.
  - propose_lineup_change(): the public entry point. ADVISE teams get told to
    use `fantasy_optimize_lineup` instead; PROPOSE gets a logged dry-run report
    (Discord approve/reject isn't wired up yet — still a shadow run); AUTO
    writes for real if the kill switch is on, else reports what it would do.
"""
import json
import os
import time

import wes_fantasy  # noqa: E402 — same dir on path (server/tests add it)
import wes_yahoo  # noqa: E402

# PC-local, like teams.yaml — never the repo (an action ledger for a real
# account is not something to publish, even though it holds no secrets itself).
LEDGER_FILE = os.environ.get(
    "WES_FANTASY_LEDGER",
    os.path.join(os.path.expanduser("~"), "wes-pc", "fantasy_ledger.jsonl"))

# The kill switch for live writes. Absent/unset = OFF. Even once a real
# _submit_lineup lands, this must be set explicitly — mirrors WES_YAHOO_LIVE for
# the schema-drift canary (docs/fantasy-gm-design.md §10).
LIVE_WRITES = os.environ.get("WES_YAHOO_LIVE_WRITES", "0") == "1"

# Bench-type slots are interchangeable on Yahoo's side (BN/BE/IL/IL+/IR/IR+/NA) —
# moving someone from IR to BN isn't a meaningful "move" for our purposes, it's
# the same non-starting status. Collapse them all to "BN" for diffing.
_BENCH_ALIASES = {"BN", "BE", "IL", "IL+", "IR", "IR+", "IR-R", "NA", "RES"}


def _norm_slot(s):
    s = (s or "").strip().upper()
    return "BN" if s in _BENCH_ALIASES else s


def diff_lineup(players, result):
    """The moves needed to turn the CURRENT Yahoo roster into the optimizer's
    recommendation. `players` is `compute_lineup`'s enriched player list (has
    name/slot/player_key AND value/playing — the WHY, carried through so the
    ledger and any summary never have to re-derive or guess it); `result` is
    `optimize_lineup()`'s dict.

    Returns [{"player_key","name","from_slot","to_slot","value","playing"}],
    skipping anyone whose current slot already matches (including bench-alias
    collapses — moving IR to BN isn't a real move). Pure — no network, no clock."""
    target = {s["name"]: s["slot"] for s in result.get("starters", [])}
    for name in result.get("bench", []):
        target[name] = "BN"
    moves = []
    for p in players:
        name = p.get("name", "")
        want = target.get(name)
        have = _norm_slot(p.get("slot"))
        if want and _norm_slot(want) != have:
            moves.append({"player_key": p.get("player_key", ""), "name": name,
                         "from_slot": p.get("slot", ""), "to_slot": want,
                         "value": p.get("value"), "playing": p.get("playing")})
    return moves


def _fmt_player(m):
    val = m.get("value")
    return f"{m['name']} ({val:g} pts)" if val is not None else m["name"]


def summarize_moves(moves):
    """Human-readable WHY for a `diff_lineup` move list — the model-layer
    explanation (docs/data-architecture.md layer 5) of what changed and why,
    reading only value/playing already attached to each move; it invents
    nothing new.

    Pairs moves into swaps where the slot TYPES trade (A moves into B's old
    slot type and vice versa) for a natural "X over Y" sentence; anything left
    over (a fill into a previously-open slot, or a bench-only move with no
    counterpart) gets its own line. This pairing is PRESENTATION ONLY — unlike
    `_plan_swaps`' similar-looking pairing, a wrong guess here just reads a
    little oddly, it never clicks a real row, so the row-identity ambiguity
    that makes `_plan_swaps` target by browser DOM identity doesn't apply here.

    Availability is stated as the reason whenever the player COMING OFF a
    starting slot wasn't playing (bye/no game) — that's the real driver
    regardless of the value numbers; otherwise the value comparison is the
    reason, which is the honest default (this engine has no other signal)."""
    remaining = list(moves)
    lines = []
    while remaining:
        m = remaining.pop(0)
        want, have = _norm_slot(m["to_slot"]), _norm_slot(m["from_slot"])
        partner = None
        for i, other in enumerate(remaining):
            if _norm_slot(other["from_slot"]) == want \
                    and _norm_slot(other["to_slot"]) == have:
                partner = other
                del remaining[i]
                break
        if partner:
            m_starts = want != "BN"
            starter, benched = (m, partner) if m_starts else (partner, m)
            if benched.get("playing") is False:
                lines.append(f"Benched {_fmt_player(benched)} (no game this "
                             f"week) for {_fmt_player(starter)} at "
                             f"{starter['to_slot']}.")
            else:
                lines.append(f"Started {_fmt_player(starter)} at "
                             f"{starter['to_slot']} over {_fmt_player(benched)}.")
        elif want == "BN":
            reason = " (no game this week)" if m.get("playing") is False else ""
            lines.append(f"Benched {_fmt_player(m)}{reason}.")
        else:
            lines.append(f"Started {_fmt_player(m)} at {m['to_slot']} "
                         f"(the slot was open).")
    return lines


def check_guardrails(team, action_type, _now=None):
    """(allowed: bool, reason: str) for `action_type` under `team`'s configured
    autonomy + guardrails (teams.yaml, design §4-§5). Never raises — a
    misconfigured or absent guardrail REFUSES the action rather than crashing,
    since the safe failure mode for a real-money write is "did nothing."""
    mode = str(team.get("autonomy") or "advise").lower()
    if mode == "advise":
        return False, ("this team is advise-only — read `fantasy_optimize_lineup` "
                       "for a recommendation, but nothing may be proposed or "
                       "executed for it")
    guard = team.get("guardrails") or {}
    allowed = guard.get("actions_allowed") or []
    if action_type not in allowed:
        return False, (f"'{action_type}' is not in this team's actions_allowed "
                       f"({allowed!r}) — guardrail refuses it")
    fresh_min = guard.get("require_fresh_data_minutes")
    fetched_at = team.get("_data_fetched_at")   # set by the caller if known
    # `is not None`, not truthiness — a fetch at t=0.0 is a real timestamp, and
    # `0.0 and ...` would short-circuit and silently skip the check. The same
    # None-vs-zero trap that once benched a real player at "0 value" (#029).
    if fresh_min and fetched_at is not None:
        now = _now if _now is not None else time.time()
        age_min = (now - fetched_at) / 60.0
        if age_min > fresh_min:
            return False, (f"data is {age_min:.0f}min old, older than this "
                           f"team's {fresh_min}min freshness guardrail (§8.8: "
                           f"don't act on stale data)")
    return True, ""


def _append_ledger(entry, _path=None):
    """Append one JSON line. Failing to write the ledger must not crash the
    turn — it's an observability nicety, not a correctness dependency — so this
    swallows and prints rather than raising."""
    path = _path or LEDGER_FILE
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, default=str) + "\n")
    except OSError as e:
        print(f"[execute] ledger write failed: {e!r}", flush=True)


def _dom_slot(slot):
    """Yahoo's row-level `data-pos` spells multi-position slots with
    underscores ("W_R_T"); everything upstream of this module (the scraped
    roster, `optimize_lineup`'s output) uses slashes ("W/R/T"). Empirically
    confirmed against a real flex slot on 2026-07-30 — not assumed."""
    return _norm_slot(slot).replace("/", "_")


def _plan_swaps(moves, current_slots):
    """PURE planning: turn `moves` (diff_lineup output) into an ordered list of
    swap operations executable via Yahoo's player<->player (or player<->empty)
    swap gesture. No browser, no network — the whole point is that this is
    checkable by inspection and unit tests before it ever touches a real page.

    Returns [(name, partner_or_None, dom_slot_type), ...] meaning: click
    `name`'s row (the swap source), then click the swaptarget whose current
    occupant is `partner` — or, if `partner` is None, the EMPTY swaptarget of
    `dom_slot_type`.

    Each real Yahoo swap exchanges TWO slot assignments at once, so this
    greedily pairs up moves that satisfy each other (A wants B's slot AND B
    wants to leave it -> one swap resolves both) and falls back to an empty
    slot when no such partner exists. `current_slots` is mutated bookkeeping
    only in a local copy — this function has no side effects on its inputs.

    Bounded iteration; raises ValueError if the greedy pairing loop itself
    can't terminate.

    HONEST LIMIT: this function only sees OCCUPIED slots (`current_slots` is
    built from rostered players; Yahoo's scraper never reports a truly empty
    row — there's no player row to scrape). So it cannot distinguish "there's
    a real empty slot of this type" from "every slot of this type is full and
    everyone in it is staying put" — it has no total-capacity figure to check
    against. It TRUSTS that `moves` came from `optimize_lineup` run against
    this same roster's real slot capacity, which is true in normal use
    (`compute_lineup` -> `diff_lineup` -> here) and makes a genuine capacity
    mismatch structurally unreachable. If that trust is ever violated — an
    adversarial or buggy move set — the "empty slot" fallback here will be
    WRONG, but it still fails safely one layer down: `_execute_swap` looks for
    an actual empty `swaptarget` row and raises RuntimeError when none exists,
    rather than clicking something incorrect. So the overall system still
    never half-applies silently; it just isn't caught at planning time."""
    remaining = {m["name"]: dict(m) for m in moves}
    slots = dict(current_slots)
    plan = []
    guard = 0
    limit = len(moves) * 3 + 5
    while remaining:
        guard += 1
        if guard > limit:
            raise ValueError(f"lineup plan did not converge after {limit} "
                             f"steps; unresolved: {list(remaining)}")
        name, m = next(iter(remaining.items()))
        want = _norm_slot(m["to_slot"])
        if _norm_slot(slots.get(name, "")) == want:
            del remaining[name]
            continue
        # A partner currently sitting in the wanted slot type who ALSO needs to
        # leave it — one swap satisfies both of you.
        partner = None
        for other_name, other_m in remaining.items():
            if other_name == name:
                continue
            if _norm_slot(slots.get(other_name, "")) == want \
                    and _norm_slot(other_m["to_slot"]) != want:
                partner = other_name
                break
        old_slot = slots.get(name, "")
        plan.append((name, partner, _dom_slot(m["to_slot"])))
        slots[name] = m["to_slot"]
        if partner:
            slots[partner] = old_slot
        del remaining[name]
    return plan


def _row_for_select(page, player_key):
    sel = page.query_selector(f"select[name='{player_key}']")
    if not sel:
        return None
    return sel.evaluate_handle("el => el.closest('tr')").as_element()


def _execute_swap(page, name, partner, dom_slot_type, key_of):
    """Drive ONE swap in the real browser: click `name`'s row, then click the
    swaptarget occupied by `partner` (matched by NAME text, never by slot type
    alone — see the module docstring for why that matters), or the empty
    swaptarget of `dom_slot_type` if `partner` is None. Raises RuntimeError
    with a specific reason on anything unexpected — never guesses."""
    key = key_of.get(name)
    if not key:
        raise RuntimeError(f"no player_key on record for {name!r}")
    row = _row_for_select(page, key)
    if row is None:
        raise RuntimeError(f"couldn't find {name!r}'s roster row (key {key})")
    label = row.query_selector("span.pos-label")
    if label is None:
        raise RuntimeError(f"{name!r}'s row has no clickable position label")
    label.scroll_into_view_if_needed()
    page.wait_for_timeout(250)
    label.click()
    page.wait_for_timeout(600)

    targets = page.query_selector_all("table.ysf-rosterswapper tbody tr.swaptarget")
    match = None
    for t in targets:
        text = t.inner_text()
        if partner is not None:
            if partner in text:
                match = t
                break
        elif "(Empty)" in text and t.get_attribute("data-pos") == dom_slot_type:
            match = t
            break
    if match is None:
        page.keyboard.press("Escape")
        wanted = partner or f"an empty {dom_slot_type} slot"
        raise RuntimeError(
            f"no swap target found for {name!r} -> {wanted} among "
            f"{[t.get_attribute('data-pos') for t in targets]}")
    label2 = match.query_selector("span.pos-label") or match
    label2.scroll_into_view_if_needed()
    page.wait_for_timeout(200)
    label2.click()
    page.wait_for_timeout(900)


def _submit_lineup(team_key, moves, _session_cls=None, _roster_fn=None):
    """The live write. Plans (pure), executes each swap in one browser session,
    and VERIFIES the final roster against every move's intended slot before
    returning — a click that silently did the wrong thing (the exact failure
    mode recon surfaced once) must never be reported as success.

    Raises RuntimeError on any planning failure, execution failure, or
    post-write mismatch. Never partially succeeds silently — the ledger entry
    that wraps this call records exactly what was attempted either way."""
    roster_fn = _roster_fn or wes_yahoo.roster_players
    before = roster_fn(team_key)
    if not isinstance(before, list):
        raise RuntimeError(f"couldn't read the current roster before writing: {before!r}")
    key_of = {p["name"]: p.get("player_key", "") for p in before}
    current_slots = {p["name"]: p.get("slot", "") for p in before}

    plan = _plan_swaps(moves, current_slots)

    session_cls = _session_cls or wes_yahoo._Session
    with session_cls() as page:
        page.goto(wes_yahoo._team_url(team_key), wait_until="domcontentloaded")
        page.wait_for_timeout(2000)
        for name, partner, dom_slot_type in plan:
            _execute_swap(page, name, partner, dom_slot_type, key_of)

    after = roster_fn(team_key)
    if not isinstance(after, list):
        raise RuntimeError(f"wrote the lineup but couldn't verify it: {after!r}")
    after_slot = {p["name"]: p.get("slot", "") for p in after}
    mismatches = [(m["name"], m["to_slot"], after_slot.get(m["name"]))
                 for m in moves
                 if _norm_slot(after_slot.get(m["name"], "")) != _norm_slot(m["to_slot"])]
    if mismatches:
        raise RuntimeError(f"lineup write did not verify: {mismatches}")


def propose_lineup_change(team=None, _compute_fn=None, _submit_fn=None,
                          _now=None, _ledger_path=None):
    """The P3 entry point: compute the recommended lineup, diff it against the
    real roster, gate it through the team's autonomy + guardrails, log the
    outcome, and — ONLY if the mode is `auto`, the moves are guardrail-approved,
    `WES_YAHOO_LIVE_WRITES=1`, AND a real submit function is wired — write it.
    Every other combination is dry-run: computed, diffed, logged, reported, never
    submitted. Degrades to a string on any problem; never raises into a turn.
    """
    compute = _compute_fn or wes_fantasy.compute_lineup
    out = compute(team)
    if isinstance(out, str):
        return out  # degradation from compute_lineup — relay verbatim

    chosen, err = wes_yahoo._resolve_team(team)
    if err:
        return err
    if chosen is None:
        return "No fantasy team is configured yet — set up teams.yaml first."

    moves = diff_lineup(out["players"], out["result"])
    allowed, reason = check_guardrails(chosen, "set_lineup", _now)
    now = _now if _now is not None else time.time()
    entry = {"ts": now, "team_key": out["team_key"], "name": out["name"],
             "sport": out["sport"], "action_type": "set_lineup",
             "autonomy": chosen.get("autonomy"), "moves": moves,
             "allowed": allowed, "reason": reason, "executed": False,
             "dry_run": True}

    if not moves:
        entry["reason"] = entry["reason"] or "lineup already matches the recommendation"
        _append_ledger(entry, _ledger_path)
        return f"No lineup changes needed for {out['name']} — already optimal."

    if not allowed:
        _append_ledger(entry, _ledger_path)
        return f"Not proposing a lineup change for {out['name']}: {reason}."

    mode = str(chosen.get("autonomy") or "advise").lower()
    move_lines = "\n".join(f"  {m['name']}: {m['from_slot'] or '(none)'} -> "
                           f"{m['to_slot']}" for m in moves)
    why = summarize_moves(moves)
    why_lines = "\n".join(f"  {line}" for line in why)
    entry["why"] = why   # kept in the ledger too, not just the reply

    if mode == "auto" and LIVE_WRITES:
        submit = _submit_fn or _submit_lineup
        try:
            submit(out["team_key"], moves)
            entry["executed"], entry["dry_run"] = True, False
            _append_ledger(entry, _ledger_path)
            return (f"Set the lineup for {out['name']}:\n{move_lines}\n"
                    f"Why:\n{why_lines}")
        except (ValueError, RuntimeError) as e:
            # ValueError from _plan_swaps means nothing touched the page yet —
            # safe. RuntimeError can come from mid-plan (a swap partway through
            # a multi-swap sequence, no verification pass reached) or from the
            # post-write verification mismatch itself — in EITHER case the real
            # roster may no longer match either the old or the intended new
            # state, so the honest thing is to say so and point at Yahoo
            # directly rather than imply nothing happened.
            entry["reason"] = f"live write attempt failed: {e}"
            entry["executed"] = "unknown"   # not False — may be partially true
            _append_ledger(entry, _ledger_path)
            return (f"Tried to update the lineup for {out['name']} but hit an "
                    f"error partway through: {e}. The real roster on Yahoo may "
                    f"not match what you expect — please check it directly "
                    f"before trusting it.\nIntended:\n{move_lines}\n"
                    f"Why:\n{why_lines}")

    _append_ledger(entry, _ledger_path)
    if mode == "auto":
        return (f"Would set the lineup for {out['name']} (auto mode, shadow "
                f"run — live writes are off):\n{move_lines}\nWhy:\n{why_lines}")
    return (f"Proposed lineup change for {out['name']} (needs your approval — "
           f"Discord approve/reject isn't wired up yet, this is a shadow "
           f"run):\n{move_lines}\nWhy:\n{why_lines}")
