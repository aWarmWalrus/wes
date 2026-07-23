"""Owner-drivable Yahoo draft-room recon (ticket #030).

The live draft room (on-the-clock indicator, countdown timer, pick button,
available-player list) is a dynamic JS app that only exists DURING a draft, so
its DOM can't be captured except from inside a live mock/real draft — and Yahoo
offers no NBA mock drafts in the offseason (confirmed 2026-07-22). This tool is
how we capture it the moment one IS reachable (preseason, ~October): it opens
the persisted logged-in browser, you navigate to the draft page yourself, and it
dumps the DOM on demand so we can write selectors — no guessing at Yahoo's
navigation, and the "join a live draft" decision stays yours.

Run ON THE PC (needs a visible browser + console, like yahoo_connect.py):

    python pc\\yahoo_draft_recon.py [start_url]

A Chrome window opens (default: the fantasy home). Navigate to the page you want
captured, come back to this console, press Enter to dump it. Repeat for each
distinct screen (lobby, the draft board, your turn "on the clock"). Type q then
Enter to quit. Captures land in  <profile>\\..\\draft_recon\\  as timestamped
<ts>_url.txt / _page.html / _probe.json triples. Nothing is joined or clicked by
this tool — it only reads whatever page you have open.
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(__file__))
import wes_yahoo as y  # noqa: E402

RECON_DIR = os.environ.get(
    "WES_DRAFT_RECON_DIR",
    os.path.join(os.path.dirname(y.PROFILE_DIR), "draft_recon"))

# JS structural probe: pre-extract the elements a draft-room scraper will need,
# so we design selectors from a small JSON digest instead of raw HTML. Keyword
# buckets are deliberately loose — recon is about discovering what's there.
_PROBE_JS = r"""() => {
    const cap = (s, n=60) => (s || '').trim().replace(/\s+/g, ' ').slice(0, n);
    const digest = el => ({
        tag: el.tagName, cls: cap(el.className, 50), id: el.id || '',
        text: cap(el.textContent, 60),
    });
    // tables (round grids / draft board / picks) — headers + a sample row
    const tables = [...document.querySelectorAll('table')].slice(0, 25).map((t, i) => ({
        i, cls: cap(t.className, 50),
        headers: [...t.querySelectorAll('th')].map(th => cap(th.textContent, 25)).slice(0, 10),
        row0: (() => { const r = t.querySelector('tbody tr');
            return r ? [...r.querySelectorAll('td')].map(td => ({
                cls: cap(td.className, 30), text: cap(td.textContent, 30),
                a: (() => { const a = td.querySelector('a');
                    return a ? { cls: cap(a.className, 25), href: a.getAttribute('href') } : null; })(),
            })) : []; })(),
    }));
    // clickable things (pick/draft/queue buttons)
    const buttons = [...document.querySelectorAll('button,[role=button],a.Btn,input[type=submit]')]
        .filter(e => /draft|pick|queue|select|auto/i.test((e.textContent || '') + ' ' + (e.className || '') + ' ' + (e.value || '')))
        .slice(0, 25).map(digest);
    // countdown / clock candidates (text like m:ss, or clock/time in class/id)
    const clocks = [...document.querySelectorAll('*')]
        .filter(e => e.children.length === 0 &&
            (/\b\d{1,2}:\d{2}\b/.test(e.textContent || '') || /clock|countdown|timer|onthe?clock/i.test((e.className || '') + ' ' + (e.id || ''))))
        .slice(0, 20).map(digest);
    // lists that could be the available-player pool
    const lists = [...document.querySelectorAll('ul,ol,[role=grid],[role=table]')]
        .filter(e => /player|available|pool|rank|queue/i.test((e.className || '') + ' ' + (e.id || '')))
        .slice(0, 15).map(digest);
    // any element referencing a Yahoo player, to see how names/positions render
    const players = [...document.querySelectorAll("a[href*='/players/'], a[href*='/nba/players/']")]
        .slice(0, 8).map(a => ({ text: cap(a.textContent, 40), href: a.getAttribute('href'),
            parentCls: cap(a.parentElement && a.parentElement.className, 40) }));
    return { url: location.href, title: document.title,
             tableCount: document.querySelectorAll('table').length,
             tables, buttons, clocks, lists, players };
}"""


def _capture(page, tag=""):
    os.makedirs(RECON_DIR, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S") + (f"_{tag}" if tag else "")
    base = os.path.join(RECON_DIR, ts)
    try:
        page.wait_for_timeout(500)  # let late JS settle
        html = page.content()
        probe = page.evaluate(_PROBE_JS)
    except Exception as e:  # noqa: BLE001
        print(f"  capture failed: {e!r}")
        return
    with open(base + "_url.txt", "w", encoding="utf-8") as f:
        f.write(page.url + "\n")
    with open(base + "_page.html", "w", encoding="utf-8") as f:
        f.write(html)
    with open(base + "_probe.json", "w", encoding="utf-8") as f:
        json.dump(probe, f, indent=1, ensure_ascii=False)
    print(f"  captured -> {base}_(url|page|probe)")
    print(f"    url: {probe.get('url')}")
    print(f"    tables={probe.get('tableCount')} "
          f"buttons={len(probe.get('buttons', []))} "
          f"clocks={len(probe.get('clocks', []))} "
          f"lists={len(probe.get('lists', []))} "
          f"players={len(probe.get('players', []))}")


def main():
    start = sys.argv[1] if len(sys.argv) > 1 else y.FANTASY_HOME
    if not y.configured():
        print("Yahoo isn't connected — run pc\\yahoo_connect.py first.")
        return 1
    print(f"Opening the logged-in browser at {start}")
    print("Navigate to the draft page you want to capture, then come back here.")
    with y._Session(headless=False) as page:
        try:
            page.goto(start, wait_until="domcontentloaded")
        except Exception as e:  # noqa: BLE001
            print(f"(couldn't load {start}: {e!r} — navigate manually)")
        n = 0
        while True:
            try:
                cmd = input("\n[Enter]=capture, tag then Enter=labeled capture, "
                            "q=quit > ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if cmd.lower() == "q":
                break
            n += 1
            _capture(page, tag=cmd or f"cap{n}")
    print(f"\nDone. {n} capture(s) in {RECON_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
