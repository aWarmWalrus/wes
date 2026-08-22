r"""Draft-room banter: read the chat, occasionally say something (#039).

WHAT MAKES THIS DIFFERENT FROM EVERY OTHER AGENT HERE
Every other model call in this project produces a CHOICE among options the
engine already verified — a pick, a lineup, an approval. This one produces free
text that goes to other human beings, in their own league, under the owner's
name. There is no shortlist to constrain it and nothing to verify it against.
So the guardrails are not about correctness, they are about restraint:

  * SILENCE IS THE DEFAULT and the most common outcome. `None` is a valid,
    frequent answer. A bot that replies to everything is the thing everyone
    mutes.
  * NEVER REPLY TO OURSELVES, or two agents in a room become an infinite loop
    with an audience. GMSnappy in the current draft is itself an AI.
  * NEVER REPLY TO SYSTEM MESSAGES. "X has joined the draft!" is the server
    talking, and answering it is how a bot announces it is a bot.
  * ONE REPLY PER MESSAGE, and a hard floor between messages. The rate limit
    is enforced in CODE, not requested in the prompt — a small model asked to
    "be brief and occasional" is not a rate limiter.
  * OFF UNLESS ASKED. `propose` is the default: it composes and logs without
    posting, so the owner can read what it WOULD have said before letting it
    speak. Posting to other people is not a thing to switch on by accident.

The reply is a suggestion from a 12b model, so it will sometimes be flat. Flat
is fine. The failure that matters is volume, not quality.
"""
import json
import os
import time
import urllib.request

OLLAMA_URL = os.environ.get("WES_OLLAMA_URL", "http://127.0.0.1:11434")
MODEL = os.environ.get("WES_BANTER_MODEL",
                       os.environ.get("WES_ESCALATE_MODEL", "gemma4:12b"))

# Floor between messages, in seconds. Deliberately long: a draft runs for an
# hour or two and a handful of well-timed lines is the whole brief.
MIN_GAP_S = 180.0

# Cap on what we will post. Sleeper chat is a one-line medium and a paragraph
# from a bot reads as spam regardless of content.
MAX_CHARS = 200

SYSTEM = (
    "You are a fantasy football manager's assistant, chatting in a live draft "
    "room with the manager's friends. You are drafting the team for them and "
    "they know it — you do not need to hide it or keep announcing it.\n"
    "Write ONE short line of friendly trash talk or a reply to what was just "
    "said. Dry and specific beats loud and generic.\n"
    "The draft context gives you REAL material, and using it is the whole "
    "job: `recent_picks` is who just took whom, `rosters_by_slot` counts each "
    "team's positions (five running backs and no tight end is a fact worth "
    "mentioning), `our_slot` is which one is yours, and `our_injuries` "
    "describes your own walking wounded. Cite something true from those "
    "rather than inventing a jab -- a specific line about a real pick beats "
    "any amount of generic swagger, and a made-up claim is just confusing.\n"
    "Rules: one sentence, under 200 characters. No hashtags. No emoji spam "
    "(one is plenty). Never insult anyone's appearance, intelligence, family, "
    "or anything about who they are — the target is always their FANTASY "
    "TEAM. Keep it the kind of ribbing friends enjoy.\n"
    "If there is nothing worth saying, say nothing: reply with an empty "
    "message. That is the normal case and it is always an acceptable answer.\n"
    'Reply as JSON: {"message": "<your line, or empty string to stay quiet>"}'
)


def _ask(payload, _post_fn=None):
    body = json.dumps({
        "model": MODEL, "stream": False, "format": "json", "think": False,
        "options": {"temperature": 0.8},   # banter, not arithmetic
        "messages": [{"role": "system", "content": SYSTEM},
                     {"role": "user", "content": json.dumps(payload)}],
    }).encode()
    try:
        if _post_fn is not None:
            raw = _post_fn(body)
        else:
            req = urllib.request.Request(
                OLLAMA_URL + "/api/chat", data=body,
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=60) as r:
                raw = json.load(r)["message"]["content"]
        got = json.loads(raw)
        return got if isinstance(got, dict) else None
    except Exception:  # noqa: BLE001 — banter must never break a draft
        return None


def new_messages(messages, seen_texts, me):
    """Messages worth reacting to: not ours, not the server, not already seen.

    `seen_texts` is a set of message texts already processed. Sleeper's chat
    carries no stable message id in the DOM, so identity is the text itself —
    imperfect (a repeated line looks old) and deliberately biased towards
    staying quiet."""
    out = []
    for m in messages or []:
        if m.get("system"):
            continue
        if (m.get("author") or "").lower() == (me or "").lower():
            continue
        if m.get("text") in seen_texts:
            continue
        out.append(m)
    return out


def compose(messages, context=None, _post_fn=None):
    """A line to post, or None to stay quiet. None is the common case."""
    if not messages:
        return None
    payload = {
        "draft": context or {},
        "recent_chat": [{"from": m.get("author"), "said": m.get("text")}
                        for m in messages[-8:]],
    }
    got = _ask(payload, _post_fn=_post_fn)
    if not isinstance(got, dict):
        return None
    line = " ".join(str(got.get("message") or "").split())
    if not line:
        return None
    if len(line) > MAX_CHARS:
        # Truncating mid-sentence reads worse than saying nothing.
        return None
    return line


class Banter:
    """Rate-limited chat participation for one draft.

    Holds the state the rate limit needs. `mode` is "off", "propose" (compose
    and report, post nothing) or "auto" (post) — the same vocabulary as team
    autonomy, so there is one idea to learn rather than two."""

    def __init__(self, draft_id, me=None, mode="propose",
                 min_gap_s=MIN_GAP_S, _now=None, browser=None):
        # An optional held-open browser. Reading the chat cost a full launch
        # every poll -- ~9s to fetch a handful of messages -- which is what
        # made the bot feel sluggish in a live room.
        self.browser = browser
        # Defaulted late, from wes_sleeper, so switching accounts is one
        # setting rather than a hunt. Getting this wrong is not cosmetic: `me`
        # is how the bot knows not to answer itself, and a mismatch turns two
        # agents in a room into an infinite loop with an audience.
        import wes_sleeper
        self.draft_id = draft_id
        self.me = me or wes_sleeper.USERNAME
        self.mode = mode
        self.min_gap_s = min_gap_s
        self.seen = set()
        self.last_sent_at = 0.0
        self._now = _now or time.time
        self.primed = False

    def tick(self, context=None, _read_fn=None, _send_fn=None, _post_fn=None):
        """One pass. Returns (action, detail) for logging.

        action is one of: "quiet", "rate_limited", "would_say", "said",
        "send_failed", "error"."""
        if self.mode == "off":
            return "quiet", "banter is off"
        try:
            # An INJECTED reader keeps the simple signature: a test stub
            # should not have to know this class grew a browser.
            msgs = (_read_fn(self.draft_id) if _read_fn
                    else _default_read(self.draft_id, self.browser))
        except Exception as e:  # noqa: BLE001 — chat must never break a draft
            return "error", f"{type(e).__name__}: {e}"

        fresh = new_messages(msgs, self.seen, self.me)
        # PRIME ON THE FIRST PASS. Everything already in the room when we
        # arrived is history, not something to answer; without this the bot
        # opens by replying to a conversation that finished an hour ago.
        self.seen.update(m["text"] for m in msgs if m.get("text"))
        if not self.primed:
            self.primed = True
            return "quiet", f"primed with {len(self.seen)} existing message(s)"
        if not fresh:
            return "quiet", "nothing new"

        since = self._now() - self.last_sent_at
        if since < self.min_gap_s:
            return "rate_limited", f"{self.min_gap_s - since:.0f}s to go"

        # Carried into the return so the log shows the EXCHANGE. A
        # transcript of only our own lines reads like a bot talking to itself,
        # which is also the failure mode worth spotting early.
        prompt = " | ".join(f"{m['author']}: {m['text'][:60]}" for m in fresh[-2:])
        line = compose(msgs, context=context, _post_fn=_post_fn)
        if not line:
            return "quiet", f"nothing worth saying (re: {prompt})"
        if self.mode != "auto":
            self.last_sent_at = self._now()   # propose mode is rate-limited too
            return "would_say", f"{line}   <- re: {prompt}"
        try:
            ok = (_send_fn(self.draft_id, line) if _send_fn
                  else _default_send(self.draft_id, line, self.browser))
        except Exception as e:  # noqa: BLE001
            return "error", f"{type(e).__name__}: {e}"
        # The clock starts whether or not it landed: a failing send that is
        # retried every poll is exactly the flood this exists to prevent.
        self.last_sent_at = self._now()
        detail = f"{line}   <- re: {prompt}"
        return ("said", detail) if ok else ("send_failed", detail)


def _default_read(draft_id, browser=None):
    import wes_sleeper
    return wes_sleeper.read_chat(draft_id, browser=browser)


def _default_send(draft_id, text, browser=None):
    import wes_sleeper
    return wes_sleeper.send_chat(draft_id, text, browser=browser)
