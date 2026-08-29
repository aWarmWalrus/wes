"""Is `disable` a property of the ROW rather than of our turn?

Every probe tonight read rows[0] and saw "disable"; the loop reads a specific
player's row. If some rows are enabled and others are not at the same instant,
then the class says something about the PLAYER (already drafted, ineligible)
and nothing about whose turn it is -- and I have been reading it as a clock.
"""
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, r"C:\Users\awarm\wes\pc")
import wes_http  # noqa: E402
from sleeper import data as sl  # noqa: E402

D = sys.argv[1]

SAMPLE = """() => {
    const rows = [...document.querySelectorAll('.player-rank-item2')];
    return rows.slice(0, 25).map((r, i) => {
        const n = r.querySelector('.name-wrapper');
        const b = r.querySelector('.draft-button');
        return {
            i: i,
            name: n ? (n.innerText || '').split(String.fromCharCode(10))[0] : '?',
            btn: b ? b.className : 'none',
        };
    });
}"""

with sl._Session() as page:
    page.set_viewport_size({"width": 1600, "height": 1000})
    sl.authenticate(page)
    page.goto(f"{sl.WEB}/draft/nfl/{D}", wait_until="domcontentloaded",
              timeout=60000)
    page.wait_for_selector(".player-rank-item2", timeout=25000)
    page.wait_for_timeout(2500)

    picks = wes_http.get_json(f"{sl.BASE}/draft/{D}/picks", ttl=0) or []
    taken_names = {
        " ".join(filter(None, [(p.get("metadata") or {}).get("first_name"),
                               (p.get("metadata") or {}).get("last_name")]))
        for p in picks}
    print(f"{len(picks)} picks made\n")
    print(f"{'i':>3} {'name':26} {'button':24} already drafted?")
    enabled = disabled = 0
    for r in page.evaluate(SAMPLE):
        off = "disable" in (r["btn"] or "")
        enabled += 0 if off else 1
        disabled += 1 if off else 0
        print(f"{r['i']:>3} {r['name'][:26]:26} {r['btn'][:24]:24} "
              f"{r['name'] in taken_names}")
    print(f"\nenabled {enabled}, disabled {disabled}")
