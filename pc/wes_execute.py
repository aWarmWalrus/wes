"""The gated executor + action ledger (ticket #029 P3, design §5).

STATUS 2026-07-30: **SHADOW-MODE ONLY.** This module can compute what it would
do, check it against guardrails, and log it — but it cannot write to Yahoo yet.
Live writes (`_submit_lineup`) are an intentional `NotImplementedError`. That is
not a placeholder oversight; it is what "shadow mode first" (§5) means in code:
this is the exact prerequisite phase the design mandates before any team goes
live, and it is genuinely useful on its own — every call builds the action
ledger's track record.

WHY WRITES AREN'T HERE YET: read-only DOM recon on 2026-07-29/30 (against the
real `nfl.l.957011.t.4` roster page) found the mechanism but not a finished,
tested write path:
  - Each roster row is `<select name="{player_key}">`; option VALUES are that
    player's eligible slot codes (including "BN"). Setting a player's slot is
    setting their select's value.
  - The edit form POSTs to `/f1/<league>/<team>/editroster`.
  - The submit control is `<input type="hidden" name="jsubmit" value="Save
    Changes" class="roster-save-btn">` — hidden until JS reveals it.
  - Confirmed (safe): setting a select's DOM value directly and dispatching a
    raw `change` event does NOT reveal the save button. Yahoo's real UI is a
    custom popover opened by clicking the position label
    (`span.pos-label[role=button]`), and THAT interaction is what the site
    actually listens for. So a faithful write needs to drive that popover, not
    the underlying `<select>` — closer to how a human uses the page, and
    (deliberately) more testable step-by-step than trying to bypass it.
  - Every recon step was verified non-destructive: reloaded the roster after
    each interaction and confirmed no slot had changed server-side.
Finishing this is its own reviewed increment — it fires real writes against a
real account and deserves a dedicated test pass (click the popover, submit,
verify the round-trip, confirm a bad submission is recoverable) rather than
being rushed in alongside this module.

WHAT THIS MODULE DOES TODAY:
  - diff_lineup(): the optimizer's recommendation vs. the CURRENT Yahoo roster,
    reduced to the moves that would actually change something.
  - check_guardrails(): autonomy mode + actions_allowed + freshness, from the
    team's teams.yaml record (design §4-§5). Degrades to (False, reason) rather
    than raising — a misconfigured guardrail should refuse the action, not crash
    the turn.
  - The ledger: append-only JSONL, PC-local (like `teams.yaml` — never the
    repo), one line per proposed/blocked/would-execute action.
  - propose_lineup_change(): the public entry point. ADVISE teams get told to
    use `fantasy_optimize_lineup` instead; PROPOSE/AUTO teams get a logged,
    dry-run report. AUTO additionally requires WES_YAHOO_LIVE_WRITES=1 AND a
    working `_submit_lineup` before it would ever do anything but log — today
    that combination is unreachable, which is intentional.
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
    recommendation. `players` is `roster_players()` output (has name/slot/
    player_key); `result` is `optimize_lineup()`'s dict.

    Returns [{"player_key","name","from_slot","to_slot"}], skipping anyone whose
    current slot already matches (including bench-alias collapses — moving IR to
    BN isn't a real move). Pure — no network, no clock."""
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
                         "from_slot": p.get("slot", ""), "to_slot": want})
    return moves


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


def _submit_lineup(team_key, moves):
    """The live write. NOT IMPLEMENTED — see the module docstring for exactly
    what recon found and what's left. Raising here (rather than silently
    no-opping) means a future caller that forgets to check LIVE_WRITES fails
    loudly instead of believing a lineup was set when it wasn't."""
    raise NotImplementedError(
        "Yahoo lineup writes aren't built yet — the click-through popover "
        "sequence needs its own tested implementation (see wes_execute.py's "
        "module docstring for the recon findings).")


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

    if mode == "auto" and LIVE_WRITES:
        submit = _submit_fn or _submit_lineup
        try:
            submit(out["team_key"], moves)
            entry["executed"], entry["dry_run"] = True, False
            _append_ledger(entry, _ledger_path)
            return f"Set the lineup for {out['name']}:\n{move_lines}"
        except NotImplementedError as e:
            entry["reason"] = f"live write not available: {e}"
            _append_ledger(entry, _ledger_path)
            return (f"Would set the lineup for {out['name']} (auto mode), but "
                    f"live writes aren't built yet:\n{move_lines}")

    _append_ledger(entry, _ledger_path)
    if mode == "auto":
        return (f"Would set the lineup for {out['name']} (auto mode, shadow "
                f"run — live writes are off):\n{move_lines}")
    return (f"Proposed lineup change for {out['name']} (needs your approval — "
           f"Discord approve/reject isn't wired up yet, this is a shadow "
           f"run):\n{move_lines}")
