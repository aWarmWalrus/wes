"""Watch ONE turn, second by second, from room-open through our pick.

Four explanations for "the click does nothing" have been wrong tonight. The
only surviving observation is that a pick worked when the room had been open
seven minutes and failed when it had been open twenty seconds -- two data
points and a guess. This produces the actual curve.

Every second it records: the API's view (status, picks), and the page's view
(is our target's row present, what its button class is, what the topmost
element at the button is, whether autopick is on). It CLICKS NOTHING until the
button has been enabled for a configurable number of consecutive reads, then
clicks and keeps recording through the commit.

Run against a mock we own so nobody else's draft pays for it.
"""
import json
import os
import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
os.environ["WES_YAHOO_LIVE_WRITES"] = "1"
os.environ.setdefault("WES_SLEEPER_USER", "GMBartimusPrime")
sys.path.insert(0, r"C:\Users\awarm\wes\pc")
import wes_http  # noqa: E402
from sleeper import data as sl  # noqa: E402

D = sys.argv[1]
SLOT = int(sys.argv[2]) if len(sys.argv) > 2 else 1
CLICK_AFTER = 2          # consecutive enabled reads before we click
OUT = (r"C:\Users\awarm\AppData\Local\Temp\claude\Z--"
       r"\351d1419-ac82-4cd9-9ec5-4a31c03c97b2\scratchpad\turn_trace.json")

SNAP = """() => {
    const rows = [...document.querySelectorAll('.player-rank-item2')];
    const first = rows[0];
    const b = first ? first.querySelector('.draft-button') : null;
    let topmost = null, covered = null;
    if (b) {
        const q = b.getBoundingClientRect();
        const el = document.elementFromPoint(q.x + q.width / 2,
                                             q.y + q.height / 2);
        topmost = el ? (el.tagName + '.' + String(el.className).slice(0, 24))
                     : 'nothing';
        covered = !(el === b || b.contains(el));
    }
    const box = document.querySelector(
        '.autopick-toggle-container input[type=checkbox]');
    return {
        rows: rows.length,
        name: first ? ((first.querySelector('.name-wrapper') || {}).innerText
                       || '').split(String.fromCharCode(10))[0] : null,
        btn: b ? b.className : null,
        topmost: topmost,
        covered: covered,
        autopick: box ? !!box.checked : null,
    };
}"""

trace = []
clicked_at = None

with sl._Session() as page:
    page.set_viewport_size({"width": 1600, "height": 1000})
    sl.authenticate(page)
    page.goto(f"{sl.WEB}/draft/nfl/{D}", wait_until="domcontentloaded",
              timeout=60000)
    page.wait_for_selector(".player-rank-item2", timeout=25000)
    t0 = time.time()
    print(f"page ready at t=0; watching (click after {CLICK_AFTER} enabled "
          f"reads)")

    enabled_run = 0
    for tick in range(180):
        now = round(time.time() - t0, 1)
        try:
            snap = page.evaluate(SNAP)
        except Exception as e:  # noqa: BLE001
            snap = {"error": f"{type(e).__name__}: {e}"}
        d = wes_http.get_json(f"{sl.BASE}/draft/{D}", ttl=0) or {}
        picks = wes_http.get_json(f"{sl.BASE}/draft/{D}/picks", ttl=0) or []
        ours = sum(1 for p in picks if p.get("draft_slot") == SLOT)
        row = dict(snap, t=now, status=d.get("status"), picks=len(picks),
                   ours=ours)
        trace.append(row)
        print(f"  t={now:6.1f}  status={row['status']:9} picks={row['picks']:3} "
              f"rows={snap.get('rows')} btn={snap.get('btn')} "
              f"cover={snap.get('covered')} auto={snap.get('autopick')}")

        btn_ok = snap.get("btn") and "disable" not in snap["btn"]
        enabled_run = enabled_run + 1 if btn_ok else 0

        if clicked_at is None and enabled_run >= CLICK_AFTER and \
                d.get("status") == "drafting":
            first = page.query_selector(".player-rank-item2")
            b = first.query_selector(".draft-button") if first else None
            if b:
                b.click()
                clicked_at = now
                print(f"  *** CLICKED at t={now} "
                      f"({snap.get('name')}) ***")
        if clicked_at is not None and now - clicked_at > 40:
            break
        time.sleep(1)

json.dump(trace, open(OUT, "w", encoding="utf-8"), indent=1)
print("\ntrace ->", OUT)
