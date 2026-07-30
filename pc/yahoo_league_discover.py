"""Enumerate the owner's Yahoo fantasy leagues across sports — READ ONLY.

WHY THIS EXISTS: `wes_yahoo.my_teams()` only looks for `a[href*='/nba/']`, so a
football (or any non-NBA) league is structurally invisible to it. An NFL league
the owner had joined therefore never appeared anywhere — not in teams.yaml, not
in the docs — even though #030's recon notes had incidentally used its id
(424494) as a mock-draft lobby path without identifying it as a real league.

This is a RECON tool, deliberately separate from wes_yahoo: it observes what
Yahoo's football dashboard actually looks like so the real sport-parameterization
of wes_yahoo (#029 P7) can be written against a verified DOM rather than an
assumed one — the same reason yahoo_draft_recon.py exists.

Run on the PC (needs the logged-in profile; opens a visible Chrome window):
    C:\\Users\\awarm\\wes-pc\\.venv\\Scripts\\python.exe Z:\\wes\\pc\\yahoo_league_discover.py

Touches nothing: navigates, reads, prints. No clicks, no forms, no writes.
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import wes_yahoo as wy  # noqa: E402

# Yahoo puts each sport on its own host AND uses a different URL path segment
# per sport (football is historically "/f1/", not "/nfl/"). The dotted key
# prefix is Yahoo's game code, which is what teams.yaml records.
SPORTS = [
    {"sport": "nba", "host": "https://basketball.fantasysports.yahoo.com",
     "path": "nba", "key": "nba", "label": "Basketball"},
    {"sport": "nfl", "host": "https://football.fantasysports.yahoo.com",
     "path": "f1", "key": "nfl", "label": "Football"},
]


def _links(page, path):
    """(league_id, team_id, link_text) for every /<path>/<league>/<team> link."""
    out = {}
    for a in page.query_selector_all(f"a[href*='/{path}/']"):
        tail = (a.get_attribute("href") or "").split(
            "fantasysports.yahoo.com")[-1].split("?")[0]
        parts = [p for p in tail.split("/") if p]
        if len(parts) >= 3 and parts[0] == path and parts[1].isdigit() \
                and parts[2].isdigit():
            name = (a.get_attribute("title") or a.inner_text() or "").strip()
            cur = out.get((parts[1], parts[2]), "")
            out[(parts[1], parts[2])] = name or cur
    return [(lg, tm, nm) for (lg, tm), nm in out.items()]


def _league_only_links(page, path):
    """League ids seen WITHOUT a team id — catches a league you belong to whose
    dashboard link doesn't carry your team (e.g. pre-draft)."""
    found = set()
    for a in page.query_selector_all(f"a[href*='/{path}/']"):
        tail = (a.get_attribute("href") or "").split(
            "fantasysports.yahoo.com")[-1].split("?")[0]
        parts = [p for p in tail.split("/") if p]
        if len(parts) >= 2 and parts[0] == path and parts[1].isdigit():
            found.add(parts[1])
    return sorted(found)


def _draft_info(page, host, path, league):
    """Best-effort draft status/date off the league home. Pure observation —
    Yahoo wording varies, so this prints what it sees instead of parsing hard."""
    try:
        page.goto(f"{host}/{path}/{league}", wait_until="domcontentloaded")
        title = page.title()
        body = page.inner_text("body")[:4000]
        hits = []
        for pat in (r"[Dd]raft[^.\n]{0,80}", r"[Aa]utopick[^.\n]{0,60}",
                    r"[Pp]re-?draft[^.\n]{0,60}"):
            hits += re.findall(pat, body)
        seen, uniq = set(), []
        for h in hits:
            h = " ".join(h.split())
            if h.lower() not in seen:
                seen.add(h.lower())
                uniq.append(h)
        return title, uniq[:12]
    except Exception as e:  # noqa: BLE001
        return f"(league page failed: {e!r})", []


def main():
    if not wy._have_playwright():
        print(wy._NO_PLAYWRIGHT)
        return 1
    if not wy.has_session():
        print(wy._NOT_CONFIGURED)
        return 1
    print(f"profile: {wy.PROFILE_DIR}")
    print("A Chrome window will open. Read-only — nothing is clicked.\n")

    with wy._Session() as page:
        for site in SPORTS:
            print("=" * 68)
            print(f"{site['label']} ({site['sport']})  {site['host']}")
            print("=" * 68)
            try:
                page.goto(site["host"], wait_until="domcontentloaded")
            except Exception as e:  # noqa: BLE001
                print(f"  dashboard failed: {e!r}\n")
                continue
            print(f"  dashboard title: {page.title()!r}")

            teams = _links(page, site["path"])
            if teams:
                print("  TEAMS FOUND:")
                for lg, tm, nm in teams:
                    key = f"{site['key']}.l.{lg}.t.{tm}"
                    print(f"    league_key: {site['key']}.l.{lg}")
                    print(f"    team_key:   {key}")
                    print(f"    link text:  {nm!r}")
            else:
                print("  no <league>/<team> links on the dashboard")

            leagues = _league_only_links(page, site["path"])
            extra = [lg for lg in leagues
                     if lg not in {t[0] for t in teams}]
            if extra:
                print(f"  league ids WITHOUT a team link: {extra}")

            for lg in sorted({t[0] for t in teams} | set(extra)):
                title, hits = _draft_info(page, site["host"], site["path"], lg)
                print(f"\n  --- league {lg} ---")
                print(f"    page title: {title!r}")
                for h in hits:
                    print(f"    saw: {h}")
            print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
