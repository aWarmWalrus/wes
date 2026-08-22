"""Reading and posting in the draft-room chat.

The chat lives behind a tab in the draft room and has NO public endpoint --
every /draft/<id>/chat path 404s -- so it is DOM or nothing.
"""
import re

from . import config, pick, session

_CHAT_AGO = re.compile(r"^\d+\s+(second|minute|hour|day|week)s?\s+ago$", re.I)


def chat_rows(page):
    """Scrape the open chat panel. Reads the page, changes nothing.

    Sleeper renders each message as `.message-text` inside a container whose
    text begins with the AUTHOR, then a relative timestamp, then "reply".
    System messages ("X has joined the draft!") have no author line at all,
    which is exactly how we tell them apart -- and not replying to the server
    announcing that someone joined is most of the value."""
    return page.evaluate("""() => {
        const out = [];
        for (const m of document.querySelectorAll('.message-text')) {
            let p = m, hops = 0, ctx = '';
            while (p && hops < 5) {
                p = p.parentElement; hops++;
                if (p && (p.innerText || '').length > (m.innerText || '').length) {
                    ctx = p.innerText; break;
                }
            }
            out.push({text: (m.innerText || '').trim(),
                      context: (ctx || '').split('\\n').slice(0, 2)});
        }
        return out;
    }""")


def parse_chat(rows):
    """Rows -> [{author, text, system}]. PURE, so the parser is testable
    without a browser."""
    out = []
    for r in rows or []:
        text = (r.get("text") or "").strip()
        if not text:
            continue
        # Accept a STRING or a list of lines. Iterating a bare string yields
        # CHARACTERS, which silently produced authors like "a" and "2" -- a
        # wrong answer wearing the shape of a right one.
        ctx = r.get("context") or []
        if isinstance(ctx, str):
            ctx = ctx.splitlines()
        author = ""
        for line in [str(x).strip() for x in ctx]:
            if line and line.lower() != "reply" and not _CHAT_AGO.match(line):
                author = line
                break
        # A SYSTEM message has no author line, so the first non-timestamp line
        # is the message itself -- and taking that as the author made the
        # server look like a participant called "aykutb has joined the draft!".
        # If the two match, nobody said it.
        if author and text.startswith(author):
            author = ""
        out.append({"author": author, "text": text, "system": not author})
    return out


def show_chat(page):
    """Bring the CHAT tab forward on a page ALREADY in the draft room.

    Idempotent: if the panel is up we leave it, so a held-open session does not
    pay a tab click and a 3s settle on every read."""
    if page.query_selector("textarea[placeholder='Enter Message']"):
        return
    tab = next((t for t in page.query_selector_all(".round-tab, .tab")
                if " ".join((t.inner_text() or "").split()).upper() == "CHAT"),
               None)
    if tab is None:
        raise RuntimeError("no CHAT tab in the draft room")
    tab.click()
    page.wait_for_timeout(2000)


def _open_chat(page, draft_id):
    page.goto(f"{config.WEB}/draft/nfl/{draft_id}",
              wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(9000)
    show_chat(page)


def read_chat(draft_id, _session_cls=None, browser=None):
    """Every message in the draft chat, oldest first. Read-only.

    `browser` is an optional held-open Browser. Reading the chat otherwise
    costs a full browser launch every time -- ~9s, paid on almost every poll of
    a chat loop -- and with a held page it is sub-second."""
    if browser is not None:
        page = browser.page()
        show_chat(page)
        return parse_chat(chat_rows(page))
    with (_session_cls or session.Session)() as page:
        page.set_viewport_size({"width": 1600, "height": 1000})
        if not session.authenticate(page):
            raise RuntimeError("no Sleeper token — cannot reach the draft")
        _open_chat(page, draft_id)
        return parse_chat(chat_rows(page))


def send_chat(draft_id, text, _session_cls=None, browser=None):
    """Post one message. True once it is VISIBLE in the panel afterwards.

    Verified by re-reading, like every other write here: `join_draft` clicked a
    live-looking control six different ways and fired no network request at
    all, so "the click did not throw" means nothing on this site."""
    body = " ".join((text or "").split())
    if not body:
        return False
    if not pick.writes_allowed():
        raise RuntimeError("Sleeper writes are off")
    if browser is not None:
        page = browser.page()
        show_chat(page)
        return post_message(page, body)
    with (_session_cls or session.Session)() as page:
        page.set_viewport_size({"width": 1600, "height": 1000})
        if not session.authenticate(page):
            raise RuntimeError("no Sleeper token — cannot reach the draft")
        _open_chat(page, draft_id)
        return post_message(page, body)


def post_message(page, body):
    """Type and send, then confirm it is visible. Shared by both paths so the
    held-open session cannot quietly diverge from the per-call one."""
    box = page.query_selector("textarea[placeholder='Enter Message']")
    if box is None:
        raise RuntimeError("no message box in the chat panel")
    box.click()
    box.fill(body)
    page.keyboard.press("Enter")
    page.wait_for_timeout(2500)
    return any(m["text"] == body for m in parse_chat(chat_rows(page)))
