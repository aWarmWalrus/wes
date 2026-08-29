"""A canonical player table with a crosswalk to every other system's ids (#039).

WHY THIS EXISTS
Sleeper publishes `espn_id`/`gsis_id`/`yahoo_id` mapping fields and **has stopped
maintaining them for newer players**: 106/300 of its own top-300 carry an
espn_id, and the missing ones are exactly the young, high-value players
(Gibbs, Bijan, Chase, Nacua — all `years_exp <= 5`). Joining on those ids alone
made 13 of the top 25 projected players invisible to the draft board, and an
absent player is not a low-ranked one, he simply never appears.

nflverse maintains the same bridge properly — `espn_id` 3993/4033 and `gsis_id`
4033/4033 for recent players — but carries no `sleeper_id`. So the ONE mapping
nobody publishes is Sleeper -> everything else, and that is what this builds.

THE DESIGN POINT THAT MATTERS
Identity is resolved ONCE, AT BUILD TIME, into a durable table — not fuzzily,
per-lookup, under a draft clock. A name match made here can be inspected,
corrected and re-used; a name match made at pick time is invisible and
unrepeatable. Ambiguities are REPORTED rather than silently resolved, because a
wrong player is worse than a missing one.

Canonical key is `gsis_id` where nflverse knows the player, since it is the id
that is actually maintained across seasons. Sleeper-only players (practice
squad, very recent signings) keep a `sleeper:<id>` key rather than being dropped.
"""
import csv
import io
import re

import wes_http

NFLVERSE_PLAYERS = ("https://github.com/nflverse/nflverse-data/releases/"
                    "download/players/players.csv")
PLAYERS_TTL = float(24 * 3600)

_SUFFIXES = (" jr", " sr", " ii", " iii", " iv", " v")


def name_key(name):
    """A normalised join key: lowercase, no punctuation, no generational suffix.

    Deliberately loose. It is only ever used TOGETHER with position, and only to
    propose a match that is then recorded — never to pick a player at draft
    time."""
    s = (name or "").lower().strip()
    s = re.sub(r"[.,'`\-]", "", s)
    for suf in _SUFFIXES:
        if s.endswith(suf):
            s = s[: -len(suf)]
    return " ".join(s.split())


def parse_nflverse(rows):
    """nflverse players.csv -> canonical records. PURE."""
    out = {}
    for r in rows:
        gsis = (r.get("gsis_id") or "").strip()
        name = (r.get("display_name") or "").strip()
        if not (gsis and name):
            continue
        out[gsis] = {
            "player_id": gsis,
            "name": name,
            "position": (r.get("position") or "").strip().upper(),
            "team": (r.get("latest_team") or r.get("team") or "").strip(),
            "birth_date": (r.get("birth_date") or "").strip(),
            "last_season": (r.get("last_season") or "").strip(),
            "gsis_id": gsis,
            "espn_id": (r.get("espn_id") or "").strip() or None,
            "pfr_id": (r.get("pfr_id") or "").strip() or None,
            "sleeper_id": None,
            "yahoo_id": None,
        }
    return out


def merge_sleeper(canon, sleeper_index):
    """Attach `sleeper_id` to canonical records. PURE.

    Returns (canon, report). Matching order is deliberate — strongest evidence
    first, and a weaker rule NEVER overrides a stronger one:

      1. espn_id / gsis_id, when Sleeper actually has them (exact)
      2. name + position + BIRTH DATE (exact enough to trust)
      3. name + position, only when it is UNAMBIGUOUS on both sides

    Rule 3 is where a mistake would come from, so a name+position that matches
    more than one canonical record is left UNMATCHED and reported. A missing
    player costs a pick; a wrong one costs a roster spot and is far harder to
    notice."""
    by_espn = {c["espn_id"]: c for c in canon.values() if c.get("espn_id")}
    by_np = {}
    for c in canon.values():
        by_np.setdefault((name_key(c["name"]), c["position"]), []).append(c)

    report = {"by_id": 0, "by_dob": 0, "by_name": 0,
              "ambiguous": [], "unmatched": []}

    for sid, sp in (sleeper_index or {}).items():
        pos = (sp.get("positions") or [None])[0]
        rec = None

        espn = sp.get("espn_id")
        if espn and str(espn) in by_espn:
            rec = by_espn[str(espn)]
            report["by_id"] += 1
        elif sp.get("gsis_id") and sp["gsis_id"] in canon:
            rec = canon[sp["gsis_id"]]
            report["by_id"] += 1
        else:
            cands = by_np.get((name_key(sp.get("name")), pos)) or []
            dob = (sp.get("birth_date") or "").strip()
            if dob:
                exact = [c for c in cands if c.get("birth_date") == dob]
                if len(exact) == 1:
                    rec = exact[0]
                    report["by_dob"] += 1
            if rec is None:
                if len(cands) == 1:
                    rec = cands[0]
                    report["by_name"] += 1
                elif len(cands) > 1:
                    report["ambiguous"].append(
                        f"{sp.get('name')} ({pos}): {len(cands)} candidates")

        if rec is None:
            # Sleeper-only: keep him, keyed on Sleeper, rather than dropping a
            # player the platform believes is real.
            canon[f"sleeper:{sid}"] = {
                "player_id": f"sleeper:{sid}",
                "name": sp.get("name"), "position": pos,
                "team": sp.get("team"), "birth_date": sp.get("birth_date", ""),
                "last_season": "", "gsis_id": None, "espn_id": sp.get("espn_id"),
                "pfr_id": None, "sleeper_id": str(sid),
                "yahoo_id": sp.get("yahoo_id"),
            }
            report["unmatched"].append(f"{sp.get('name')} ({pos})")
        else:
            rec["sleeper_id"] = str(sid)
            if not rec.get("yahoo_id"):
                rec["yahoo_id"] = sp.get("yahoo_id")
    return canon, report


def build(_nflverse_fn=None, _sleeper_fn=None):
    """Fetch both sources and produce (table, report)."""
    from sleeper import data as wes_sleeper
    text = (_nflverse_fn or _fetch_nflverse)()
    canon = parse_nflverse(list(csv.DictReader(io.StringIO(text))))
    index = (_sleeper_fn or wes_sleeper.players_index)()
    return merge_sleeper(canon, index)


def _fetch_nflverse():
    return wes_http.get_text(NFLVERSE_PLAYERS, ttl=PLAYERS_TTL, timeout=90)


def index_by(table, key):
    """{external id -> canonical record}, skipping records without that id."""
    return {str(r[key]): r for r in table.values() if r.get(key)}
