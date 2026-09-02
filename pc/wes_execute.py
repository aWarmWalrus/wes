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
  - Nothing here is reachable without `WES_YAHOO_LIVE_WRITES=1` AND that
    ACTION's autonomy being `auto` (or, for add_drop, an explicit owner
    approval naming both players) AND the action being in `actions_allowed` —
    three independent gates, not one. Autonomy is PER ACTION since 2026-08-01
    (`autonomy_for`), so running lineups unattended does not imply permission
    to drop anyone; note that the scalar form still means "all actions", which
    is why `add_drop` must ALSO be listed in actions_allowed to be reachable.

WHAT THIS MODULE DOES:
  - diff_lineup(): the optimizer's recommendation vs. the CURRENT Yahoo roster,
    reduced to the moves that would actually change something.
  - autonomy_for(): this team's mode FOR ONE ACTION — risk is per action, not
    per team (a bad lineup self-corrects next run; a drop never does).
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
  - propose_roster_moves(): the same for drop/add (#035). Writes on `auto`, or
    on an `approve={"drop","add"}` that still matches what the engine would do
    right now — a stale approval is REFUSED, never substituted.
"""
import json
import os
import threading
import time

import wes_fantasy  # noqa: E402 — same dir on path (server/tests add it)
import wes_nfl  # noqa: E402 — recent form + valuation (#035)
import wes_yahoo  # noqa: E402

# PC-local, like teams.yaml — never the repo (an action ledger for a real
# account is not something to publish, even though it holds no secrets itself).
# The configured default, kept separate because the test suite monkeypatches
# LEDGER_FILE to a tmp path (conftest, autouse) and there would otherwise be no
# way left to assert what the real default IS.
DEFAULT_LEDGER_FILE = os.environ.get(
    "WES_FANTASY_LEDGER",
    os.path.join(os.path.expanduser("~"), "wes-pc", "fantasy_ledger.jsonl"))
LEDGER_FILE = DEFAULT_LEDGER_FILE

# The kill switch for live writes. Absent/unset = OFF. Even once a real
# _submit_lineup lands, this must be set explicitly — mirrors WES_YAHOO_LIVE for
# the schema-drift canary (docs/fantasy-gm-design.md §10).
LIVE_WRITES = os.environ.get("WES_YAHOO_LIVE_WRITES", "0") == "1"

# Per-REQUEST write suppression, on top of the process-wide kill switch above.
#
# The nightly eval drives the LIVE server, and one golden case ("check if my
# team needs lineup changes") makes the model call the executor. LIVE_WRITES is
# read by the SERVER process, so nothing the eval sets in its own environment
# can stop that -- and it was writing to the real Yahoo account at 03:36 every
# eval night (found 2026-08-14 by diffing executed-write timestamps against the
# scheduler's triggers). A test suite must not mutate a live account.
#
# threading.local, not a ContextVar: Flask serves each request (including the
# consumption of a streaming generator) on one thread, so thread-local state
# covers the whole request. The server sets it from a header and clears it on
# teardown, so it cannot leak into the next request on a recycled thread.
_local = threading.local()


def set_writes_suppressed(on):
    _local.suppressed = bool(on)


def writes_suppressed():
    return getattr(_local, "suppressed", False)


def writes_enabled():
    """The single question every write path asks. Reads the module attribute at
    call time so tests can still monkeypatch LIVE_WRITES."""
    return LIVE_WRITES and not writes_suppressed()

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


# --- roster recommendation (#035 R3) -----------------------------------------
# PURE. Given rostered players (with recent form) and available players (with
# value), propose drop/add pairs. Deliberately NOT built on _plan_swaps: a
# lineup swap trades two slots inside a FIXED roster, this changes roster
# MEMBERSHIP. They look alike and have different invariants — exactly the
# resemblance that invites a subtle bug.
#
# Positional need is a hard constraint, not a preference: dropping the only
# kicker to add a fourth receiver is strictly worse regardless of points.
def _primary_pos(player):
    pos = player.get("positions") or []
    return pos[0] if pos else ""


def recommend_roster_moves(roster, available, min_gain=2.0, limit=3,
                           protected=()):
    """Propose drop/add pairs, worst-recent-form out, best-available in.

    `roster` entries need {name, positions, value, form} where `form` is
    `wes_nfl.recent_form` output; `available` entries need {name, positions,
    value}. Returns [{drop, drop_pos, drop_form, add, add_pos, gain, reason}],
    best gain first, at most `limit`.

    RULES, each of which exists to prevent a specific bad drop:
      * Same primary position only — keeps the roster legal (see above).
      * UNKNOWN form or UNKNOWN value never justifies a drop. `recent_form`
        returns None below its games floor, and an unrated player's value is
        None; treating either as 0 would drop a player for having no data
        rather than for being bad. This is the same None-is-not-zero rule that
        already governs the optimizer.
      * `min_gain` — the add must beat the drop by a real margin. Churning the
        roster for +0.3 projected points is noise, and every move spends a
        limited weekly budget.
      * `protected` names are never dropped, mirroring never_drop so a caller
        that forgets the guardrail still can't propose a protected drop.
    Pure — no network, no clock, no I/O."""
    protected_keys = {_norm_name_key(n) for n in protected}
    # Weakest recent form first; anyone without a usable form reading is not a
    # drop candidate at all.
    candidates = []
    for p in roster or []:
        form = p.get("form") or {}
        recent = form.get("recent_ppg")
        if recent is None:
            continue
        if _norm_name_key(p.get("name")) in protected_keys:
            continue
        candidates.append((recent, p))
    candidates.sort(key=lambda t: t[0])

    by_pos = {}
    for a in available or []:
        if a.get("value") is None:
            continue
        by_pos.setdefault(_primary_pos(a), []).append(a)
    for lst in by_pos.values():
        lst.sort(key=lambda a: a["value"], reverse=True)

    recs, taken = [], set()
    for recent, drop in candidates:
        pos = _primary_pos(drop)
        for add in by_pos.get(pos, []):
            if add["name"] in taken:
                continue
            gain = round(add["value"] - recent, 2)
            if gain < min_gain:
                break          # list is sorted; nothing further will qualify
            taken.add(add["name"])
            form = drop.get("form") or {}
            recs.append({
                "drop": drop["name"], "drop_pos": pos,
                "drop_form": recent, "drop_baseline": form.get("baseline_ppg"),
                "add": add["name"], "add_pos": _primary_pos(add),
                "add_value": add["value"], "gain": gain,
                "reason": (f"{drop['name']} is averaging {recent:g} over recent "
                           f"games (season {form.get('baseline_ppg', '?')}); "
                           f"{add['name']} is available and projects "
                           f"{add['value']:g}"),
            })
            break
        if len(recs) >= limit:
            break
    recs.sort(key=lambda r: r["gain"], reverse=True)
    return recs[:limit]


def summarize_roster_moves(recs):
    """Plain-language WHY for `recommend_roster_moves` output — the model-layer
    explanation, reading only what the recommender already computed."""
    lines = []
    for r in recs:
        base = r.get("drop_baseline")
        drift = (f", down from {base:g} on the season"
                 if isinstance(base, (int, float)) else "")
        lines.append(
            f"Drop {r['drop']} ({r['drop_pos']}, {r['drop_form']:g} pts in "
            f"recent games{drift}) for {r['add']} ({r['add_value']:g}) — "
            f"about +{r['gain']:g} points.")
    return lines


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


# --- roster-move guardrails (#035 R4) ----------------------------------------
# never_drop / max_moves_per_week / max_faab_bid_pct were declared in teams.yaml
# from the start and read by NO code — harmless while set_lineup was the only
# action (it neither drops anyone nor spends FAAB), but they became load-bearing
# the moment a roster move is possible, because a DROP IS IRREVERSIBLE: another
# manager can claim the player within minutes and no guardrail un-drops them.
#
# max_moves_per_week is the first guardrail that depends on HISTORY rather than
# config, so it reads the ledger. That makes it the first one that can be wrong
# because of a missing file rather than a bad setting — hence: an unreadable
# ledger REFUSES the move (fail closed), it does not assume zero moves so far.
_WEEK_SECONDS = 7 * 24 * 3600


def _norm_name_key(name):
    return "".join(c for c in str(name or "").lower() if c.isalnum())


def count_recent_moves(team_key, since_ts, _path=None, _entries_fn=None):
    """How many roster moves this team actually EXECUTED since `since_ts`.

    Counts ledger entries with `executed` True or "unknown" — an uncertain
    write must count AGAINST the budget, since it may well have happened.
    Returns None if the ledger can't be read, which callers must treat as
    "unknown, refuse", never as zero.

    A row SUPERSEDED by a later correction (some later row carries
    `correction_of_ts` pointing at it) is not counted. The ledger is
    append-only, so a corrected row stays on disk forever; without this, a
    correction inflates the count by one and eats the weekly budget. That
    happened for real: correcting the 2026-08-01 over-reported add/drop made
    two executed moves look like three, and the team then refused every roster
    move from 08-05 to 08-07 at a cap it had never actually reached."""
    if _entries_fn is not None:
        entries = _entries_fn()
    else:
        path = _path or LEDGER_FILE
        if not os.path.exists(path):
            return 0          # no ledger yet = genuinely no moves, not unknown
        try:
            entries = []
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        entries.append(json.loads(line))
        except (OSError, json.JSONDecodeError):
            return None       # unreadable -> unknown, caller must fail closed
    def _ref(e):
        """The row this one restates, as a float — or None."""
        raw = e.get("correction_of_ts")
        if raw is None:
            return None
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None       # junk reference supersedes nothing; count both

    # Built from ALL entries, not just the window: a correction can land after
    # the row it supersedes has aged past `since_ts`, and it must still cancel
    # it rather than being counted as a move of its own.
    superseded = {r for r in (_ref(e) for e in entries) if r is not None}

    def _when(e):
        """When the MOVE happened. A correction is a restatement of a past
        event, not a new one, so it is windowed at the time of the row it
        corrects — otherwise fixing an old record would re-charge this week's
        budget for a move made weeks ago."""
        return _ref(e) if _ref(e) is not None else float(e.get("ts") or 0)

    return sum(
        1 for e in entries
        if e.get("team_key") == team_key
        and e.get("action_type") in ("add_drop", "waiver_claim")
        and e.get("executed") in (True, "unknown")
        and float(e.get("ts") or 0) not in superseded
        and _when(e) >= since_ts)


def check_roster_move(team, drop_name, add_name=None, _now=None,
                      _ledger_path=None, _count_fn=None, enforce_cap=True):
    """(allowed, reason) for dropping `drop_name` (optionally for `add_name`).

    Layered on top of `check_guardrails` — autonomy and actions_allowed still
    apply — and adds the roster-specific rails that only matter once something
    irreversible is possible:
      * never_drop  — an explicit protected list, matched loosely on name so a
                      punctuation difference ("Ja'Marr" vs "JaMarr") can't
                      defeat protection.
      * max_moves_per_week — a real count from the ledger, not a config echo.
    Never raises; refuses on anything it cannot verify.

    `enforce_cap=False` skips ONLY the weekly cap, for a move the owner
    explicitly approved: since 2026-08-07 that cap bounds UNATTENDED moves, and
    an approved move is by definition attended. never_drop and every other rail
    still apply — this is not a general override."""
    allowed, reason = check_guardrails(team, "add_drop", _now)
    if not allowed:
        return False, reason

    guard = team.get("guardrails") or {}
    protected = {_norm_name_key(n) for n in (guard.get("never_drop") or [])}
    if _norm_name_key(drop_name) in protected:
        return False, (f"{drop_name} is on this team's never_drop list — "
                       f"refusing to drop them")

    cap = guard.get("max_moves_per_week")
    if cap is not None and enforce_cap:
        now = _now if _now is not None else time.time()
        count = (_count_fn or count_recent_moves)(
            team.get("team_key", ""), now - _WEEK_SECONDS, _ledger_path)
        if count is None:
            return False, ("couldn't read the action ledger, so this team's "
                           "max_moves_per_week cap can't be verified — "
                           "refusing rather than risking exceeding it")
        if count >= int(cap):
            return False, (f"this team has already made {count} roster "
                           f"move(s) this week, at its max_moves_per_week "
                           f"cap of {cap}")
    return True, ""


def autonomy_for(team, action_type):
    """This team's autonomy mode FOR ONE ACTION: 'advise' | 'propose' | 'auto'.

    `autonomy` may be a scalar (applies to every action) or a per-action map:

        autonomy:
          set_lineup: auto
          add_drop: propose

    Per-action is the honest shape, because risk is per action, not per team: a
    bad lineup self-corrects on the next run, a drop never does. The scalar form
    used to be the only one, and the gap it left is exactly why an `execute`
    boolean got bolted onto propose_roster_moves — the config couldn't say "run
    my lineups but ask me before dropping anyone" (#035, 2026-08-01).

    Unknown/missing actions fall back to 'advise', the mode that never acts."""
    mode = team.get("autonomy")
    if isinstance(mode, dict):
        # A per-action map that doesn't mention this action has not granted it.
        mode = mode.get(action_type)
    mode = str(mode or "advise").lower()
    return mode if mode in ("advise", "propose", "auto") else "advise"


def check_guardrails(team, action_type, _now=None):
    """(allowed: bool, reason: str) for `action_type` under `team`'s configured
    autonomy + guardrails (teams.yaml, design §4-§5). Never raises — a
    misconfigured or absent guardrail REFUSES the action rather than crashing,
    since the safe failure mode for a real-money write is "did nothing."

    NOTE this answers "may this action be taken at all", not "should it happen
    unattended" — `advise` is refused here, but `propose` and `auto` both pass.
    Callers decide between those two via `autonomy_for`."""
    mode = autonomy_for(team, action_type)
    if mode == "advise":
        return False, (f"this team is advise-only for '{action_type}' — read "
                       f"`fantasy_optimize_lineup` for a recommendation, but "
                       f"nothing may be proposed or executed for it")
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


def record_action(entry, _path=None):
    """Append one action to the shared ledger — the public door to
    `_append_ledger`, for callers outside this module.

    ONE ledger for every action WES takes on a fantasy team, whatever the
    platform. A Sleeper draft pick and a Yahoo add/drop answer the same
    question later — "what did it do, and why did it think that was right?" —
    and splitting them per platform would mean two things to search and two
    things to keep in step.

    Rows carry `action_type`, and `count_recent_moves` counts only add_drop and
    waiver_claim, so a new action type cannot silently eat the weekly move
    budget."""
    _append_ledger(entry, _path)


def _read_ledger(_path=None):
    """Every ledger row, oldest first. [] if unreadable — this backs a
    conversational lookup, so a missing ledger should answer "nothing on
    record", never raise into a turn."""
    path = _path or LEDGER_FILE
    if not os.path.exists(path):
        return []
    rows = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        rows.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue      # one bad line must not hide the rest
    except OSError:
        return []
    return rows


def recent_actions(team=None, limit=5, include_skipped=False, _path=None):
    """What WES actually DID, newest first, in natural language.

    The ledger is the durable record of every write; conversation memory is
    not. Before this existed, "why did you drop him?" could only be answered
    from the chat window — so a restart, a window roll, or (as actually
    happened) the nightly eval clearing the channel left Jarvis unable to
    explain a move it had made hours earlier and DMed about (2026-08-09).

    Relays the `why` that was COMPUTED AND STORED at decision time rather than
    re-deriving it. Re-deriving would explain yesterday's decision using today's
    numbers, which is how an audit trail starts quietly lying.

    `include_skipped` also reports runs that decided to do nothing — that is
    what answers "why didn't you do anything this week?".

    Degrades to a string; never raises."""
    rows = _read_ledger(_path)
    if not rows:
        return "I don't have any fantasy actions on record yet."

    # Same supersession rule as count_recent_moves: a corrected row and its
    # correction are ONE event, and listing both would report a move twice.
    superseded = set()
    for e in rows:
        ref = e.get("correction_of_ts")
        if ref is not None:
            try:
                superseded.add(float(ref))
            except (TypeError, ValueError):
                pass

    want = (team or "").strip().lower()
    did_rows, skipped_rows = [], []
    for e in rows:
        if float(e.get("ts") or 0) in superseded:
            continue
        if want and want not in str(e.get("name", "")).lower():
            continue
        if e.get("executed") in (True, "unknown"):
            did_rows.append(e)
        else:
            skipped_rows.append(e)

    # Real moves are NEVER displaced by no-ops. The scheduler runs four times a
    # week and most runs change nothing, so a plain newest-first list under a
    # limit buried actual moves under a wall of "no action" — the model then
    # told the owner nothing had happened when it had (caught live 2026-08-09).
    # Executed actions get the list; skipped runs get one summary line.
    did_rows.sort(key=lambda e: float(e.get("ts") or 0), reverse=True)
    skipped_rows.sort(key=lambda e: float(e.get("ts") or 0), reverse=True)

    if not did_rows and not (include_skipped and skipped_rows):
        scope = f" for {team}" if team else ""
        return f"I haven't made any fantasy moves{scope} that I have a record of."

    lines = []
    for e in did_rows[:max(1, int(limit or 5))]:
        when = time.strftime("%a %b %d %I:%M%p",
                             time.localtime(float(e.get("ts") or 0)))
        name = e.get("name", "your team")
        why = e.get("why") or []
        if e.get("executed") == "unknown":
            head = f"{when} — tried a change to {name}, but the write errored"
        else:
            head = f"{when} — {name}"
        detail = "; ".join(why) if why else (e.get("reason")
                                             or "no detail recorded")
        lines.append(f"{head}: {detail}")

    if not lines:
        lines.append("No moves were actually made.")
    if include_skipped and skipped_rows:
        newest = skipped_rows[0]
        when = time.strftime("%a %b %d %I:%M%p",
                             time.localtime(float(newest.get("ts") or 0)))
        why = "; ".join(newest.get("why") or []) or (newest.get("reason")
                                                     or "no detail recorded")
        lines.append(f"({len(skipped_rows)} other run(s) changed nothing; "
                     f"most recent {when}: {why})")
    return "\n".join(lines)


def _dom_slot(slot):
    """Yahoo's row-level `data-pos` spells multi-position slots with
    underscores ("W_R_T"); everything upstream of this module (the scraped
    roster, `optimize_lineup`'s output) uses slashes ("W/R/T"). Empirically
    confirmed against a real flex slot on 2026-07-30 — not assumed."""
    return _norm_slot(slot).replace("/", "_")


# Which positions a slot will accept. Yahoo spells flex slots as "W/R/T"; the
# single letters are its own abbreviations, so they are mapped explicitly
# rather than guessed at.
_SLOT_LETTER = {"Q": "QB", "W": "WR", "R": "RB", "T": "TE", "K": "K",
                "D": "DEF", "DEF": "DEF", "DST": "DEF"}


def _slot_accepts(slot):
    """The set of positions `slot` can hold, or None for 'anything'."""
    s = _norm_slot(slot)
    if s in ("BN", "IR", ""):
        return None                      # bench and IR take anyone
    parts = [p for p in s.split("/") if p]
    out = set()
    for p in parts:
        out.add(_SLOT_LETTER.get(p, p))
    return out or None


def _can_fill(slot, positions):
    """Could a player eligible at `positions` legally sit in `slot`?

    UNKNOWN MEANS YES. Yahoo's roster scrape returns an empty eligibility list
    for some players -- Ja'Marr Chase came back `eligible=[]` on a live read --
    so treating "we don't know" as "not allowed" would block legal swaps and
    be far worse than the bug this exists to fix."""
    accepts = _slot_accepts(slot)
    if accepts is None or not positions:
        return True
    return any(p in accepts for p in positions)


def _plan_swaps(moves, current_slots, eligible=None):
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
    want_of = {m["name"]: _norm_slot(m["to_slot"]) for m in moves}
    elig = eligible or {}
    slots = dict(current_slots)
    plan = []
    guard = 0
    limit = len(moves) * 3 + 5

    def settled(n):
        return _norm_slot(slots.get(n, "")) == want_of[n]

    def legal(a, b):
        """Both halves of the exchange have to be allowed, or Yahoo simply
        will not offer the row."""
        return (_can_fill(slots.get(b, ""), elig.get(a))
                and _can_fill(slots.get(a, ""), elig.get(b)))

    while True:
        guard += 1
        if guard > limit:
            raise ValueError(f"lineup plan did not converge after {limit} "
                             f"steps; unresolved: "
                             f"{[n for n in want_of if not settled(n)]}")
        unresolved = [n for n in want_of if not settled(n)]
        if not unresolved:
            return plan

        # A LEGAL SWAP THAT MAKES PROGRESS. The old rule looked only for a
        # partner already sitting in the slot `name` wants, which is right for
        # a straight two-player exchange and wrong for a cycle: it paired
        # Ja'Marr Chase (WR) with Rhamondre Stevenson (BN) because Stevenson
        # occupied the bench slot Chase wanted -- an exchange that would have
        # put a RB in a WR slot. Yahoo never offered the row, and the write
        # failed every morning from 2026-08-26.
        #
        # Any partner will do provided the exchange is legal both ways and
        # lands at least one of the two on its target. The same three moves
        # then decompose as Chase<->Collins (both WR-eligible) followed by
        # Chase<->Stevenson (a flex slot takes the RB) -- same end state, two
        # swaps Yahoo will actually perform.
        pick = None
        for a in unresolved:
            for b in want_of:
                if b == a or not legal(a, b):
                    continue
                # A SWAP WITHIN ONE SLOT TYPE CHANGES NOTHING. Exchanging two
                # bench players leaves both on the bench, so the state after is
                # identical to the state before -- and the progress test below
                # still called it progress, because a partner ALREADY on its
                # target trivially satisfies "lands on its target".
                #
                # That is an infinite loop, not a slow one: the planner picked
                # Cairo Santos <-> Tony Pollard (both BN) every iteration,
                # never changed a thing, and burned the whole step budget
                # without ever reaching the empty-slot fallback that would
                # have put Santos in the vacant K. It failed the entire lineup
                # write on Teletubbies 2026-09-02 -- including the two moves
                # that were perfectly valid.
                if _norm_slot(slots.get(a, "")) == _norm_slot(slots.get(b, "")):
                    continue
                a_lands, b_lands = slots.get(b, ""), slots.get(a, "")
                if (_norm_slot(a_lands) == want_of[a]
                        or _norm_slot(b_lands) == want_of[b]):
                    pick = (a, b)
                    break
            if pick:
                break

        if pick:
            a, b = pick
            a_to, b_to = slots.get(b, ""), slots.get(a, "")
            # `_execute_swap` matches the target row by PARTNER NAME, so the
            # slot recorded here is only used for the empty-slot case below.
            # Record where `a` actually lands rather than where it wants to be:
            # in a cycle those differ, and a misleading label is how the next
            # person debugging this loses an hour.
            plan.append((a, b, _dom_slot(a_to)))
            slots[a], slots[b] = a_to, b_to
            continue

        # NO PARTNER: aim at an empty slot of the wanted type. See the honest
        # limit above -- planning cannot prove one exists, and `_execute_swap`
        # refuses rather than clicking something wrong when it does not.
        a = unresolved[0]
        plan.append((a, None, _dom_slot(want_of[a])))
        slots[a] = want_of[a]


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


def _submit_add_drop(team_key, add_player_key, drop_player_key,
                     _session_cls=None, _roster_fn=None):
    """Execute ONE add/drop against Yahoo. **This is the first irreversible
    action in the system** — a dropped player can be claimed by another manager
    within minutes and no amount of retrying gets them back.

    Mechanism (recon 2026-07-31, verified non-destructive at every step by
    re-reading the roster afterward):
      - `/f1/<league>/addplayer?apid=<id>` opens the add FORM (a GET; commits
        nothing — confirmed, roster unchanged after loading it).
      - That form is `POST /f1/<league>/<team>/addplayer` with `apid` (the
        player to add) and a `dpid` checkbox (the player to drop). Both ids are
        the same Yahoo player keys `roster_players` already returns, so nothing
        new has to be resolved.

    Drives the real controls rather than posting a synthetic form, for the same
    reason `_execute_swap` does: Yahoo's own JS/crumb handling stays in play.
    Re-reads the roster afterward and RAISES unless the add and the drop BOTH
    actually took effect — a partial result here is a real roster in an
    unexpected state, which must never be reported as success."""
    roster_fn = _roster_fn or wes_yahoo.roster_players
    before = roster_fn(team_key)
    if not isinstance(before, list):
        raise RuntimeError(
            f"couldn't read the roster before an add/drop: {before!r}")
    before_keys = {p.get("player_key", "") for p in before}
    if drop_player_key not in before_keys:
        raise RuntimeError(
            f"refusing to drop player_key {drop_player_key!r}: not on the "
            f"roster we just read (stale recommendation?)")

    league_key = f"{wes_yahoo._sport_of(team_key)}.l." \
                 f"{wes_yahoo._league_of(team_key)}"
    add_url = (f"{wes_yahoo._home(wes_yahoo._sport_of(team_key))}/"
               f"{wes_yahoo._site(wes_yahoo._sport_of(team_key))['path']}/"
               f"{wes_yahoo._league_of(team_key)}/addplayer"
               f"?apid={add_player_key}")

    session_cls = _session_cls or wes_yahoo._Session
    with session_cls() as page:
        page.goto(add_url, wait_until="domcontentloaded")
        page.wait_for_timeout(2000)
        # The `dpid` checkbox is HIDDEN — Yahoo styles a real button over it
        # (`.add-drop-trigger-btn[data-check-box-value]`, title "Click to drop
        # this player"). Checking the input directly times out on "element is
        # not visible"; driving the visible control is both what works and what
        # keeps Yahoo's own JS in play, the same lesson as the lineup swapper.
        # NOTE the attribute value carries a trailing space ("34085 "), so it is
        # compared stripped rather than matched with a CSS selector.
        trigger = None
        for btn in page.query_selector_all(".add-drop-trigger-btn"):
            if (btn.get_attribute("data-check-box-value") or "").strip()                     == str(drop_player_key):
                trigger = btn
                break
        if trigger is None:
            raise RuntimeError(
                f"the add form didn't offer {drop_player_key!r} as a drop "
                f"option — refusing to guess a different player")
        trigger.scroll_into_view_if_needed()
        page.wait_for_timeout(200)
        trigger.click()
        page.wait_for_timeout(1500)

        # Clicking the trigger may complete the transaction on its own or reveal
        # a confirm step; handle both rather than assuming which.
        for sel in ("button:has-text('Add Player')",
                    "input[value='Add Player']",
                    "button:has-text('Confirm')",
                    "form[action*='addplayer'] input[type=submit]"):
            el = page.query_selector(sel)
            if el and el.is_visible():
                el.click()
                page.wait_for_timeout(2000)
                break
        page.wait_for_timeout(2000)

    after = roster_fn(team_key)
    if not isinstance(after, list):
        raise RuntimeError(
            f"submitted an add/drop but couldn't verify it: {after!r}")
    after_keys = {p.get("player_key", "") for p in after}
    if drop_player_key in after_keys:
        raise RuntimeError(
            f"add/drop did not verify: {drop_player_key!r} is still rostered")
    if add_player_key not in after_keys:
        raise RuntimeError(
            f"add/drop did not verify: {add_player_key!r} was not added "
            f"(the drop may still have gone through — check Yahoo)")
    _ = league_key   # (kept for future waiver-claim URLs; unused for a plain add)


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
    # WHAT EACH PLAYER MAY LEGALLY OCCUPY. `positions` is the field the Yahoo
    # scrape actually returns (["RB"], ["WR"], ...); it was already there and
    # planning simply never read it, which is how a swap that put a RB in a WR
    # slot got planned at all. `eligible` is accepted first only so a caller
    # with a richer roster shape can supply one -- nothing produces it today.
    eligible = {p["name"]: (p.get("eligible") or p.get("positions") or [])
                for p in before}

    plan = _plan_swaps(moves, current_slots, eligible)

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


# --- IL/IR stashing (#035 R6) ------------------------------------------------
# The optimizer never targets an IR slot (it emits real starting slots or BN),
# so an injured starter goes to the BENCH and the league's IR slots sit empty
# forever — a long-term injury permanently occupies a bench spot. That only
# becomes worth fixing once a roster spot can actually be USED, which is what
# R5 provides: stash the injured player on IR, and the freed bench spot is a
# real pickup.
#
# Yahoo only permits an IR slot for a player whose STATUS qualifies (IR/PUP/
# NFI/O varies by league setting), so this proposes rather than assumes: it
# reports who is eligible and what it would free, and the actual move rides the
# same swap machinery as any other slot change.
_IL_ELIGIBLE_STATUS = {"IR", "IR-R", "PUP", "NFI", "O", "OUT", "SUSP", "DNP"}


def il_candidates(roster, slots):
    """Rostered players who are injured, NOT already on an IL/IR slot, and
    whose status qualifies them for one — i.e. bench spots that could be freed.

    Returns [{name, player_key, status, current_slot}]. Pure. Empty when the
    league has no IL/IR slots at all, since stashing needs somewhere to stash."""
    il_slots = [s for s in (slots or [])
                if _norm_slot(s) == "BN" and str(s).strip().upper() not in
                ("BN", "BE")]
    if not il_slots:
        return []
    occupied = sum(1 for p in (roster or [])
                   if str(p.get("slot", "")).strip().upper() not in ("BN", "BE")
                   and _norm_slot(p.get("slot")) == "BN")
    if occupied >= len(il_slots):
        return []          # every IL slot already in use
    out = []
    for p in roster or []:
        slot = str(p.get("slot", "")).strip().upper()
        if _norm_slot(slot) == "BN" and slot not in ("BN", "BE"):
            continue       # already stashed
        status = str(p.get("status", "")).strip().upper()
        if status in _IL_ELIGIBLE_STATUS:
            out.append({"name": p.get("name", ""),
                        "player_key": p.get("player_key", ""),
                        "status": status, "current_slot": p.get("slot", "")})
    return out


def summarize_il_candidates(cands, il_slot="IR"):
    return [f"Move {c['name']} ({c['status']}) to {il_slot} — frees a bench "
            f"spot for a pickup." for c in cands]


def propose_roster_moves(team=None, approve=None, _roster_fn=None,
                         _fa_fn=None, _pool_fn=None, _scoring_fn=None,
                         _gamelog_fn=None, _submit_fn=None, _now=None,
                         _ledger_path=None):
    """#035 entry point: find underperformers, check who's available, and
    recommend (or, gated, execute) a drop/add pair.

    Writing happens on exactly two paths:

    * **`autonomy.add_drop: auto`** — unattended, on the scheduled run. True
      full auto; the weekly cap is the blast radius.
    * **`approve={"drop": name, "add": name}`** — the owner said yes to a
      specific pair. It is checked against the CURRENT top recommendation and
      refused if it no longer matches.

    That check is the point of naming the players rather than passing a bare
    `execute=True` flag, which is what this used to take. A flag authorises
    "whatever recs[0] is at this instant" — and recs[0] really did change
    between a suggestion and its approval once, when a cached empty ESPN page
    silently degraded the pool. It also means the local 12b has to echo the two
    names it heard, which is both better-conditioned than a bare boolean and,
    unlike a boolean, checkable.

    Degrades to a string on any problem; never raises into a turn."""
    chosen, err = wes_yahoo._resolve_team(team)
    if err:
        return err
    if chosen is None:
        return "No fantasy team is configured yet — set up teams.yaml first."
    team_key = chosen.get("team_key", "")
    league_key = chosen.get("league_key", "") or team_key
    name = chosen.get("name", "?")
    if str(chosen.get("sport") or wes_yahoo._sport_of(team_key)).lower() != "nfl":
        return (f"Roster management is NFL-only so far — {name} is not an NFL "
                f"team.")

    mode = autonomy_for(chosen, "add_drop")
    # A half-filled approval is a caller bug, and the action is irreversible, so
    # it refuses rather than guessing the missing half from the recommendation.
    if approve is not None:
        if not (isinstance(approve, dict) and approve.get("drop")
                and approve.get("add")):
            return ("To make a roster move I need both halves named — who to "
                    "drop and who to add. Nothing was changed.")
    # THE CAP DEGRADES AUTONOMY, IT DOES NOT STOP THE TEAM (owner decision,
    # 2026-08-07). At `max_moves_per_week` an `auto` team drops to `propose`:
    # it keeps finding moves and keeps saying so, it just stops acting alone.
    #
    # The old behaviour — refuse outright — meant the team went SILENT on
    # hitting the cap, and stayed silent while looking healthy. That happened
    # for real over 08-05..08-07 (see #035): seven identical refusals nobody
    # saw, because `fantasy_watch` only DMs on CHANGE, so a system stuck
    # refusing is invisible by construction.
    #
    # NOTE this narrows what the cap guarantees: it now bounds UNATTENDED moves
    # per week, not total moves — an explicit approval goes through regardless.
    # That keeps the property the cap actually exists for (bounding a runaway
    # executor bug, which is unattended by definition) while letting the owner
    # act as often as they like.
    # Checked HERE rather than via check_roster_move, which answers a composite
    # question — only the cap degrades autonomy. Every other refusal it makes
    # (advise-only, action not in actions_allowed, never_drop) is a real "no"
    # and must stay one.
    capped, cap_used, cap_max = False, None, None
    if mode == "auto" and approve is None:
        cap_max = (chosen.get("guardrails") or {}).get("max_moves_per_week")
        if cap_max is not None:
            _t = _now if _now is not None else time.time()
            cap_used = count_recent_moves(team_key, _t - _WEEK_SECONDS,
                                          _ledger_path)
            # An unreadable ledger is UNKNOWN, so it degrades too — it must
            # never be read as "zero moves used" and license another write.
            if cap_used is None or cap_used >= int(cap_max):
                mode, capped = "propose", True
    execute = mode == "auto" or approve is not None

    roster = (_roster_fn or wes_yahoo.roster_players)(team_key)
    if not isinstance(roster, list):
        return roster
    if not roster:
        return "That roster came back empty."
    available = (_fa_fn or wes_yahoo.free_agents)(league_key)
    if not isinstance(available, list):
        return available

    scoring = (_scoring_fn or wes_fantasy.nfl_league_scoring)(league_key)
    pool, _failed = (_pool_fn or wes_nfl.pool_by_position)()
    ranked = wes_nfl.rank_by_points([wes_nfl.per_game(p) for p in pool],
                                    scoring["weights"], scoring["tiers"])
    by_key = {_norm_name_key(p["name"]): p for p in ranked}
    gamelog = _gamelog_fn or wes_nfl.player_gamelog

    # Recent form for ROSTERED players only. The free-agent pool is ranked on
    # season value instead: a gamelog is one HTTP call PER PLAYER, so pulling
    # them for the whole available list would be dozens of requests for
    # candidates most of which are never considered (#035).
    enriched = []
    for p in roster:
        match = by_key.get(_norm_name_key(p.get("name")))
        form = {"recent_ppg": None}
        if match and match.get("espn_id"):
            form = wes_nfl.recent_form(gamelog(match["espn_id"]),
                                       scoring["weights"], scoring["tiers"])
        enriched.append({**p, "value": match["value"] if match else None,
                        "form": form})
    avail = []
    for a in available:
        if not a.get("is_free_agent"):
            continue      # waiver claims are a different action; not yet built
        match = by_key.get(_norm_name_key(a.get("name")))
        avail.append({**a, "value": match["value"] if match else None})

    guard = chosen.get("guardrails") or {}
    recs = recommend_roster_moves(enriched, avail,
                                  protected=guard.get("never_drop") or ())
    if not recs:
        # An EXECUTE request that finds nothing is worth recording: the owner
        # asked for an action and got a no-op. That combination previously
        # vanished without a trace, which made a real bug (a transiently empty
        # ESPN page cached for the whole season TTL) look like the system had
        # simply changed its mind — see #035, 2026-07-31.
        if execute:
            _append_ledger({"ts": _now if _now is not None else time.time(),
                            "team_key": team_key, "name": name, "sport": "nfl",
                            "action_type": "add_drop", "autonomy": mode,
                            "moves": [], "executed": False, "dry_run": True,
                            "reason": "execute requested but no move qualified"},
                           _ledger_path)
        return (f"No roster moves worth making for {name} — nobody has fallen "
                f"off enough for an available player to be a clear upgrade."
                + (" (You asked me to make the move, but the check came back "
                   "empty this time — if you saw a suggestion earlier, ask me "
                   "to check again.)" if approve is not None else ""))

    why = summarize_roster_moves(recs)
    # R6: an injured player stashed on IL frees a bench spot, which is only
    # worth surfacing now that a freed spot can actually be filled.
    try:
        il = il_candidates(roster, wes_fantasy.nfl_league_slots(league_key))
    except Exception:  # noqa: BLE001 — advisory only, never break the answer
        il = []
    if il:
        why = why + summarize_il_candidates(il)
    body = "\n".join(f"  {line}" for line in why)
    now = _now if _now is not None else time.time()
    entry = {"ts": now, "team_key": team_key, "name": name, "sport": "nfl",
             "action_type": "add_drop", "autonomy": mode,
             "moves": recs, "why": why, "executed": False, "dry_run": True}

    if not execute:
        if capped:
            # Say WHY it stopped acting, or a degraded team is indistinguishable
            # from a cautious one — which is the whole failure this replaces.
            entry["reason"] = (f"weekly cap reached ({cap_used}/{cap_max}) — "
                               f"degraded from auto to propose")
            entry["capped"] = True
            _append_ledger(entry, _ledger_path)
            used = "couldn't read the ledger" if cap_used is None else \
                f"already made {cap_used} move(s)"
            return (f"Roster moves worth considering for {name}:\n{body}\n"
                    f"(I've {used} this week, at this team's cap of {cap_max}, "
                    f"so I'm suggesting rather than acting. Say the word and "
                    f"I'll make one — approving it doesn't count against the "
                    f"cap, which only limits what I do unattended.)")
        entry["reason"] = f"recommendation only (autonomy.add_drop={mode})"
        _append_ledger(entry, _ledger_path)
        return (f"Roster moves worth considering for {name}:\n{body}\n"
                f"(Recommendation only — nothing was dropped or added. Drops "
                f"are permanent, so this one needs your say-so: tell me who to "
                f"drop and who to add.)")

    # ONE move per run: `top` is the only pair submitted below. The summary and
    # the ledger must therefore describe `top` ALONE. Reporting the whole `recs`
    # list under "Made this roster move" claimed a drop that never happened and
    # wrote it to the audit trail as executed — caught 2026-08-01 by diffing the
    # real roster against the report. The rest are still surfaced, as NOT done.
    top = recs[0]
    # THE approval check. An approved pair must still be the move the engine
    # would make right now; if the data moved underneath us, refuse and re-ask
    # rather than dropping whoever happens to be top of the list. This lived in
    # a one-off script before it lived here, which was the tell that the old
    # `execute=True` signature was wrong.
    if approve is not None:
        want = (_norm_name_key(approve["drop"]), _norm_name_key(approve["add"]))
        match = next((r for r in recs
                      if (_norm_name_key(r["drop"]),
                          _norm_name_key(r["add"])) == want), None)
        if match is None:
            entry["reason"] = (f"approved pair {approve['drop']} -> "
                               f"{approve['add']} is no longer recommended")
            _append_ledger(entry, _ledger_path)
            return (f"I didn't make that move: dropping {approve['drop']} for "
                    f"{approve['add']} isn't what the numbers say right now. "
                    f"Nothing was changed. Current thinking:\n"
                    + "\n".join(f"  {line}"
                                for line in summarize_roster_moves(recs)))
        top = match

    why = summarize_roster_moves([top])
    body = "\n".join(f"  {line}" for line in why)
    entry["moves"], entry["why"] = [top], why
    rest = ""
    if len(recs) > 1:
        rest = ("\nAlso worth considering (not done):\n"
                + "\n".join(f"  {line}"
                            for line in summarize_roster_moves(recs[1:])))

    allowed, reason = check_roster_move(chosen, top["drop"], top["add"],
                                        _now=_now, _ledger_path=_ledger_path,
                                        enforce_cap=approve is None)
    if not allowed:
        entry["allowed"], entry["reason"] = False, reason
        _append_ledger(entry, _ledger_path)
        return f"Not making a roster move for {name}: {reason}.\n{body}" + rest
    entry["allowed"] = True

    if not writes_enabled():
        entry["reason"] = "live writes are off"
        _append_ledger(entry, _ledger_path)
        return (f"Would make this roster move for {name} (live writes are "
                f"off):\n{body}" + rest)

    drop_key = next((p.get("player_key", "") for p in roster
                     if p.get("name") == top["drop"]), "")
    add_key = next((a.get("player_key", "") for a in available
                    if a.get("name") == top["add"]), "")
    if not drop_key or not add_key:
        entry["reason"] = "couldn't resolve Yahoo player ids"
        _append_ledger(entry, _ledger_path)
        return (f"Found a move for {name} but couldn't resolve the Yahoo "
                f"player ids, so nothing was done:\n{body}" + rest)

    submit = _submit_fn or _submit_add_drop
    try:
        submit(team_key, add_key, drop_key)
        entry["executed"], entry["dry_run"] = True, False
        _append_ledger(entry, _ledger_path)
        return f"Made this roster move for {name}:\n{body}" + rest
    except (ValueError, RuntimeError) as e:
        entry["reason"] = f"add/drop failed: {e}"
        entry["executed"] = "unknown"
        _append_ledger(entry, _ledger_path)
        return (f"Tried a roster move for {name} but hit an error: {e}. A drop "
                f"is permanent, so check the real roster on Yahoo before "
                f"assuming either way.\nIntended:\n{body}" + rest)


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
             "autonomy": autonomy_for(chosen, "set_lineup"), "moves": moves,
             "allowed": allowed, "reason": reason, "executed": False,
             "dry_run": True}

    if not moves:
        entry["reason"] = entry["reason"] or "lineup already matches the recommendation"
        _append_ledger(entry, _ledger_path)
        return f"No lineup changes needed for {out['name']} — already optimal."

    if not allowed:
        _append_ledger(entry, _ledger_path)
        return f"Not proposing a lineup change for {out['name']}: {reason}."

    mode = autonomy_for(chosen, "set_lineup")
    move_lines = "\n".join(f"  {m['name']}: {m['from_slot'] or '(none)'} -> "
                           f"{m['to_slot']}" for m in moves)
    why = summarize_moves(moves)
    why_lines = "\n".join(f"  {line}" for line in why)
    entry["why"] = why   # kept in the ledger too, not just the reply

    if mode == "auto" and writes_enabled():
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
