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


import re

_DEBUGGER_RE = re.compile(r"\bdebugger\b")
DEBUG = os.environ.get("WES_DRAFT_DEBUG") == "1"

# Runs in EVERY page/frame/popup BEFORE any site script (race-free), so it also
# catches the DYNAMIC anti-debug variants a network rewrite can't — the ones
# that build the debugger via the Function constructor or eval rather than a
# literal `debugger;` in a static file (`Function("debugger")()`,
# `(function(){}).constructor("debugger")()`, `eval("debugger")`).
_ANTIDEBUG_INIT = r"""
(() => {
  const RE = /\bdebugger\b/g, strip = v => typeof v === 'string' ? v.replace(RE, ';') : v;
  const OF = Function, P = new Proxy(OF, {
    apply: (t, s, a) => Reflect.apply(t, s, a.map(strip)),
    construct: (t, a, n) => Reflect.construct(t, a.map(strip), n),
  });
  try { window.Function = P; } catch (e) {}
  try { Object.defineProperty(Function.prototype, 'constructor',
        { value: P, writable: true, configurable: true }); } catch (e) {}
  const E = window.eval;
  try { window.eval = function (s) { return E.call(this, strip(s)); }; } catch (e) {}
})();
"""


def _neutralize_debugger_traps(context):
    """Defeat the anti-inspection `debugger;` traps in Yahoo's draft client that
    freeze the page under automation (Playwright drives Chrome over the DevTools
    protocol, so `debugger;` pauses; the draft-client popup stays blank). Three
    layers, because a single one keeps missing this trap:
      1) init script (above) — neuters DYNAMICALLY built debugger (the likely
         culprit here: nothing literal in the static scripts to rewrite);
      2) network rewrite — strips a literal `debugger` from static JS/HTML;
      3) CDP auto-resume — if a pause still slips through, resume it immediately
         so the page never stays frozen (and, with WES_DRAFT_DEBUG=1, log WHERE
         it paused to draft_recon\\debug.log so we can pinpoint the source).
    All best-effort; any failure falls through so the tool still runs."""
    try:
        context.add_init_script(_ANTIDEBUG_INIT)
    except Exception:  # noqa: BLE001
        pass

    def _dlog(*a):
        if not DEBUG:
            return
        try:
            os.makedirs(RECON_DIR, exist_ok=True)
            with open(os.path.join(RECON_DIR, "debug.log"), "a", encoding="utf-8") as f:
                f.write(" ".join(str(x) for x in a) + "\n")
        except Exception:  # noqa: BLE001
            pass

    def handler(route):
        try:
            if route.request.resource_type not in ("script", "document"):
                return route.continue_()
            resp = route.fetch()
            body = resp.text()
            if "debugger" in body:
                _dlog("[rewrote debugger in]", route.request.url[:120])
                body = _DEBUGGER_RE.sub("void 0", body)
            headers = {k: v for k, v in resp.headers.items()
                       if k not in ("content-encoding", "content-length")}
            return route.fulfill(status=resp.status, headers=headers, body=body)
        except Exception as e:  # noqa: BLE001
            _dlog("[fetch/route fail]", route.request.url[:100], repr(e)[:50])
            try:
                return route.continue_()
            except Exception:  # noqa: BLE001
                return

    context.route("**/*", handler)

    def _arm_cdp(pg):
        _dlog("[page]", getattr(pg, "url", "?"))
        try:
            s = context.new_cdp_session(pg)
            s.send("Debugger.enable")

            def _paused(ev):
                fr = (ev.get("callFrames") or [{}])[0]
                _dlog("[PAUSE]", ev.get("reason"),
                      f"{fr.get('url', '?')[:110]}:{fr.get('location', {}).get('lineNumber', '?')}")
                try:
                    s.send("Debugger.resume")
                except Exception:  # noqa: BLE001
                    pass
            s.on("Debugger.paused", _paused)
        except Exception:  # noqa: BLE001
            pass

    for pg in context.pages:
        _arm_cdp(pg)
    context.on("page", _arm_cdp)  # popups (the draft client) + new tabs


def main():
    # Default to FOOTBALL, not y.FANTASY_HOME (basketball): #030 is NFL-first,
    # NFL mock drafts are the ones live today, and the owner's pre-draft league
    # (nfl.l.424494) is a football league. Pass a URL to override.
    start = sys.argv[1] if len(sys.argv) > 1 else y._home("nfl")
    if not y.configured():
        print("Yahoo isn't connected — run pc\\yahoo_connect.py first.")
        return 1
    print(f"Opening the logged-in browser at {start}")
    print("Navigate to the draft page you want to capture, then come back here.")
    with y._Session(headless=False) as page:
        _neutralize_debugger_traps(page.context)
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
