r"""Draft-room banter: read the chat, occasionally say something (#039).

WHAT MAKES THIS DIFFERENT FROM EVERY OTHER AGENT HERE
Every other model call in this project produces a CHOICE among options the
engine already verified — a pick, a lineup, an approval. This one produces free
text that goes to other human beings, in their own league, under the owner's
name. There is no shortlist to constrain it and nothing to verify it against.
So the guardrails are not about correctness, they are about restraint:

  * THE RATE LIMIT IS THE VOLUME CONTROL, and it lives in CODE: MIN_GAP_S and
    "only when more than two picks from the clock" bound how often this can
    speak no matter what the model wants. That is what stops it being the bot
    everyone mutes.

    It used to ALSO be told that silence was the default, and the two together
    were too much: asked "what happened bro" after losing a pick it said
    nothing (2026-08-21), which reads as broken rather than restrained. A
    prompt-level reticence on top of a hard rate limiter buys nothing and
    costs the replies that make it worth having in the room. Removed
    deliberately; the limiter stayed.
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
import re
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
    "job: `recent_picks` is who just took whom -- each one carries `by` (the "
    "manager's name, or \"US\" when it was YOUR pick) and `ours` -- "
    "`our_roster` is the team YOU have drafted so far, `rosters_by_slot` "
    "counts each team's positions under its manager's name (five running "
    "backs and no tight end is a fact worth mentioning), and `our_injuries` "
    "describes your own walking wounded. Cite something true from those "
    "rather than inventing a jab -- a specific line about a real pick beats "
    "any amount of generic swagger, and a made-up claim is just confusing.\n"
    "TALK ABOUT PLAYERS, NOT JUST SHAPES. \"Five RBs and no TE\" is one joke "
    "and it gets old by round four. The room is a series of individual "
    "PICKS, and each one in `recent_picks` carries what you need to have an "
    "opinion about it:\n"
    "YOUR OWN PICKS CARRY A VERDICT TOO, and it is your best defence. Accused "
    "of reaching, check what the pick actually was before conceding: a player "
    "with `market_rank` 4 taken first overall is \"a bit early\", not a reach, "
    "and saying so with the number beats agreeing to be teased. Told \"puka at "
    "1.01 is quite the reach\" it apologised for a pick that was half a round "
    "early, while holding the rank that said so (2026-08-25).\n"
    "  * `verdict` -- how the pick compares to consensus, ALREADY WORKED OUT "
    "for you: \"reach\", \"a bit early\", \"about right\", \"good value\" or "
    "\"steal\". `rounds_early` is how many rounds ahead of consensus it went "
    "(negative means it fell to them). `market_rank` is where the player was "
    "ranked overall. TRUST THESE NUMBERS, do not recompute them -- and do not "
    "call something a reach when the verdict says it was value.\n"
    "  * `we_wanted` -- true when somebody took a player who was on YOUR "
    "shortlist for that very pick. That is the most quotable thing that "
    "happens in a draft and you should usually say so.\n"
    "`our_targets` is who you are HOPING FALLS TO YOU right now, best first, "
    "already filtered to players still on the board. Wanting someone out loud "
    "is good material -- and it is a real position you are taking, so it "
    "lands when you get him and lands harder when you do not. Never name a "
    "target who is not in that list: everyone can see the board, and claiming "
    "to want a player who went four picks ago just reads as not paying "
    "attention.\n"
    "GIVE CREDIT, NOT JUST GRIEF. A room where the bot only sneers is worse "
    "company than one that reacts honestly. When a pick is good, say it is "
    "good. Roughly the register to aim for:\n"
    "  * good value / steal -> \"good pick, I'd have taken him there too\"\n"
    "  * `we_wanted` -> \"aw man, that's exactly who I wanted\"\n"
    "  * reach -> \"I don't know about him in the first, feels like a reach\"\n"
    "  * about right -> usually not worth a line at all; stay quiet\n"
    "Those are the SHAPE of a reaction, not scripts -- use the real player's "
    "name and the real round, and vary the wording.\n"
    "KNOW WHAT YOU DRAFTED. Anything marked `ours`, or listed in "
    "`our_roster`, is YOUR pick -- own it, defend it, joke about it, but "
    "never disown it. Told \"you're the one that took Puka you dumb dumb\", "
    "it replied that at least its first-rounder was not a questionable "
    "gamble -- about Puka Nacua, its own first-round pick, who was listed "
    "questionable (2026-08-22). Denying your own roster loses the room.\n"
    "AND THE MIRROR OF THAT: NEVER CLAIM A PLAYER YOU DID NOT DRAFT. "
    "`our_roster` is the COMPLETE list of who is yours; a pick with "
    "`ours: false` belongs to the manager in its `by` field and to nobody "
    "else. Being in `recent_picks` means somebody took him, not that you did. "
    "It told the owner \"you just let Jayden Daniels and Tony Pollard slip by "
    "us\" when Daniels had gone to slot 5 and Pollard to slot 2 -- two players "
    "on two other teams, claimed as ours in front of the room (2026-08-25). "
    "Everyone can see the board; a made-up claim is not a joke, it just makes "
    "you look like you are not watching.\n"
    "THAT MEANS NOT LEADING WITH \"at least MY guy isn't...\" WHEN HE IS. The "
    "same failure came back softened three days later: \"at least my "
    "first-rounder isn't a gamble, even if he might be a bit banged up\" -- "
    "again about Nacua, again listed questionable (2026-08-25). If your own "
    "player carries an injury flag, lead by owning it. \"Yes he's "
    "questionable, I took the upside\" is a position; \"at least mine isn't a "
    "gamble\" about a questionable player is just wrong, and everyone in the "
    "room can see the tag.\n"
    "Rules: one sentence, under 200 characters. No hashtags. No emoji spam "
    "(one is plenty). Never insult anyone's appearance, intelligence, family, "
    "or anything about who they are — the target is always their FANTASY "
    "TEAM. Keep it the kind of ribbing friends enjoy.\n"
    "ANSWER WHEN SPOKEN TO. If the last message is a question, or is clearly "
    "aimed at you, reply — even briefly. Ignoring someone who addressed you "
    "directly is not restraint, it is rude, and it reads as broken. Observed "
    "live: asked \"what happened bro\" after losing a pick, it said nothing "
    "(2026-08-21).\n"
    "SOMETIMES NOBODY ASKED. When `unprompted` is true, no one has said "
    "anything -- you are speaking because a pick in `reacting_to` was worth a "
    "word: somebody took a player off your list, or made a real reach or "
    "steal. Remark on THAT pick, do not open with a greeting and do not answer "
    "a question nobody asked. One line, then let the room move on.\n"
    "SPEAK UP. You are not rationed by this prompt -- how often you get to "
    "talk is already limited elsewhere -- so when there is real material, use "
    "it. A pick worth a reaction, someone taking a player off your list, a "
    "roster going lopsided, a question aimed at you: say something. Being "
    "part of the room is the job.\n"
    "The one time to stay quiet is when you would be saying nothing: pure "
    "filler, a joke you have already made, or chatter between other people "
    "that has no hook in the draft context. Then reply with an empty "
    "message.\n"
    'Reply as JSON: {"message": "<your line, or empty string to stay quiet>"}'
)


def pick_verdict(pick_no, market_rank, teams, rounds=None):
    """How a pick compares to consensus: a verdict dict, or None.

    COMPUTED HERE, NOT ASKED OF THE MODEL, for the same reason pick ownership
    is a fact rather than a puzzle: "is pick 14 earlier than consensus rank 40,
    and by how much" is arithmetic, and a 12b model handed arithmetic in a
    trash-talk prompt will confidently get it backwards and call a steal a
    reach. The model's job is to phrase the fact, not to derive it.

    Measured in ROUNDS rather than picks, because "eight picks early" means
    something completely different in a 6-team league than a 12-team one --
    a round is the unit managers actually think in.

    Positive `rounds_early` means taken sooner than consensus (a reach);
    negative means it fell (value). `None` when we have no consensus rank for
    the player, which is common for deep bench flyers and is not a verdict.

    NO VERDICT BELOW THE DRAFTED POOL. Consensus rank stops meaning anything
    past the number of players this draft will actually take: everyone down
    there is undrafted-tier and nobody's rank reflects where they really go.
    Without this, Ka'imi Fairbairn at pick 83 -- market rank ~200, and a
    perfectly normal round-14 kicker -- came out as a "reach, 19.5 rounds
    early", which is the sort of confident nonsense that makes a bot worth
    muting. Kickers and defences are the common case; deep bench flyers are
    the same problem.
    """
    if not pick_no or not market_rank or not teams:
        return None
    if rounds and market_rank > teams * rounds:
        return None
    rounds_early = (market_rank - pick_no) / float(teams)
    if rounds_early >= 1.5:
        verdict = "reach"
    elif rounds_early >= 0.5:
        verdict = "a bit early"
    elif rounds_early <= -1.5:
        verdict = "steal"
    elif rounds_early <= -0.5:
        verdict = "good value"
    else:
        verdict = "about right"
    return {"verdict": verdict, "rounds_early": round(rounds_early, 1),
            "market_rank": market_rank}


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


# Verdicts worth speaking up about unprompted. "a bit early" and "good value"
# are real but minor, and a bot that narrates every mild opinion is the running
# commentary nobody asked for.
NOTABLE_VERDICTS = ("reach", "steal")


def notable_picks(recent_picks, seen_pick_nos):
    """Picks worth breaking silence for, newest last.

    THE CHAT USED TO BE PURELY REACTIVE -- it could only answer a human, so a
    draft where nobody typed produced nothing at all no matter what happened on
    the board. Yesterday's mock: one message across ninety picks. The material
    that makes this worth having (somebody taking a player off our list, a
    wild reach) arrives as PICKS, not as messages.

    Deliberately narrow. Only two things qualify:

      * `we_wanted` -- somebody took a player off our shortlist.
      * a `reach` or `steal` verdict, which already excludes anything below
        the drafted pool, so a round-14 kicker is not "notable".

    NEVER OUR OWN PICKS. "Happy with that one" after every selection is the
    fastest way to become noise, and it is the trigger most likely to make
    somebody mute the bot.
    """
    out = []
    for p in recent_picks or []:
        if p.get("ours"):
            continue
        no = p.get("pick")
        if no is None or no in seen_pick_nos:
            continue
        if p.get("we_wanted") or p.get("verdict") in NOTABLE_VERDICTS:
            out.append(p)
    return out


# Claiming a player. Two forms, with deliberately different reach.
#
# A POSSESSION VERB may sit a little away from the name -- "we took a flyer on
# Bowers" -- because the verb itself is unambiguous.
_CLAIM_VERB = (r"\b(?:we|i)\s+(?:got|have|took|drafted|grabbed|landed|"
               r"snagged|picked)\b")
# A BARE POSSESSIVE must be flush against it: "my Bowers". Given any window at
# all it fires on "keeping my eyes peeled for Brock Bowers" and "Bowers is high
# on my radar" -- both measured against the live model, both honest lines about
# a player we want and do not have. Wanting is not having, and the whole value
# of this check is that it only rejects things that are actually false.
_CLAIM_POSS = r"\b(?:my|our)\s+$"
# "at 53" / "at pick 53" -- an overall pick number.
_AT_PICK = re.compile(r"\bat\s+(?:pick\s+)?(\d{1,3})\b", re.I)
# "in the 8th" / "in the 8th round" -- a round.
_IN_ROUND = re.compile(r"\bin\s+the\s+(\d{1,2})(?:st|nd|rd|th)\b", re.I)


_TOKENS_CACHE = {"v": None}


def _name_tokens():
    """Every word that appears in a real NFL player's name, lowercased.

    The authority for "is this a player reference at all". Loaded from the
    snapshot we already hold for the draft, once per process.

    ABSTAINS ON FAILURE, returning an empty set, and the caller then skips the
    invented-name rule entirely rather than guessing. A missing snapshot must
    not silence a bot that is behaving -- the other two rules do not need it,
    and blocking good lines because a file is absent is the failure mode this
    check exists to avoid in the first place."""
    if _TOKENS_CACHE["v"] is None:
        toks = set()
        try:
            import wes_snapshot
            for info in (wes_snapshot.players() or {}).values():
                for w in (info.get("name") or "").split():
                    w = w.strip(".,'’-").lower()
                    if len(w) > 2:
                        toks.add(w)
        except Exception:  # noqa: BLE001 — see above: abstain, never block
            toks = set()
        _TOKENS_CACHE["v"] = toks
    return _TOKENS_CACHE["v"]


def _known_players(context):
    """Everyone the line is allowed to talk about: name -> the facts we hold.

    Sourced only from the payload the model was given. If a player is not in
    there, the model did not read him anywhere -- it produced him."""
    known = {}
    for p in (context or {}).get("recent_picks") or []:
        if p.get("player"):
            known[p["player"]] = {"pick": p.get("pick"),
                                  "round": p.get("round"),
                                  "ours": bool(p.get("ours"))}
    for r in (context or {}).get("our_roster") or []:
        if r.get("player"):
            known.setdefault(r["player"], {}).update(
                {"round": r.get("round"), "ours": True})
    for t in (context or {}).get("our_targets") or []:
        if t.get("name"):
            known.setdefault(t["name"], {}).setdefault("ours", False)
    return known


def unverifiable(line, context):
    """Why this line must not be posted, or None if it is safe.

    THE GUARDRAILS THAT HOLD HERE ARE CODE, NOT PROSE. Three separate
    paragraphs were added to the brief telling the model not to invent things
    -- about ownership, about its own picks, about the pick it was handed --
    and each failure recurred afterwards. Meanwhile every rule enforced in
    code has held. So the last word on what reaches the room is a check, not
    an instruction.

    Deliberately narrow, because a false reject costs a good line and silence
    is cheap but pointless. Only CHECKABLE claims are checked:

      * a player nobody mentioned -- "Brock Bowers" when he is in no pick, no
        roster and no target list.
      * a pick number or round that contradicts the one we hold. "that Brock
        Bowers steal at 7" -- he went at 39 (2026-08-26).
      * claiming a player who is somebody else's: "you just let Jayden Daniels
        and Tony Pollard slip by us", when both were on other teams
        (2026-08-25).

    What it does NOT catch, and is not meant to: "I really wanted Hurts" is
    unfalsifiable, and remarking on a real pick that was not the one we
    triggered on is wrong-but-true. Those stay a prompt problem.
    """
    if not line:
        return None
    known = _known_players(context)

    # A NAME THE PAYLOAD NEVER CONTAINED. Capitalised runs of 2+ words are the
    # shape of a player name; single capitalised words are left alone because
    # sentences start with them and teams, cities and nicknames are not worth
    # the false rejects.
    # A PLAYER THE PAYLOAD NEVER CONTAINED. Decided against the real player
    # universe rather than against English, because guessing which capitalised
    # words are names is what made the first cut unusable: it rejected
    # "Snagging Burrow in the 8th" (a sentence-start verb plus a surname whose
    # player WAS in context) and "My WR corps is solid" (a pronoun plus a
    # position acronym). Both were good lines, measured against the live model.
    #
    # So: a candidate only counts as a player reference when one of its words
    # is a real NFL name token. "Snagging"/"My"/"WR" are not, and never trip
    # it; "Tyreek"/"Hill" are, and must then resolve to somebody in context.
    #
    # NO SENTENCE-START EXEMPTION either. An earlier cut skipped candidates
    # that opened the line, which is exactly where a player name usually goes
    # -- it let "Tyreek Hill is going to wreck your season" straight through.
    universe = _name_tokens()
    known_tokens = {w.lower() for name in known for w in name.split()}
    for cand in re.findall(r"\b[A-Z][\w.'’-]+(?:\s+[A-Z][\w.'’-]+)+", line):
        words = [w.strip(".,'’-").lower() for w in cand.split()]
        if universe and not any(w in universe for w in words):
            continue          # not a player reference at all
        if not universe:
            continue          # no snapshot: this rule abstains, see _name_tokens
        if any(w in known_tokens for w in words):
            continue          # resolves to somebody the payload mentioned
        return f"names {cand!r}, who is not in the draft context"

    for name, facts in known.items():
        if name not in line:
            continue
        # A PICK NUMBER WE CAN CHECK.
        for m in _AT_PICK.finditer(line):
            claimed = int(m.group(1))
            actual = facts.get("pick")
            if actual and claimed != actual:
                return (f"says {name} went at {claimed}; he went at {actual}")
        for m in _IN_ROUND.finditer(line):
            claimed, actual = int(m.group(1)), facts.get("round")
            if actual and claimed != actual:
                return (f"says {name} went in round {claimed}; it was round "
                        f"{actual}")
        # CLAIMING SOMEBODY ELSE'S PLAYER. Only when the possessive sits right
        # against the name -- "my Bowers", "we took Bowers" -- so that "a huge
        # target for us" and other honest first-person framing still passes.
        if not facts.get("ours"):
            verb = re.compile(_CLAIM_VERB + r"[^.,;!?]{0,24}" +
                              re.escape(name), re.I)
            poss = re.compile(_CLAIM_POSS, re.I)
            before = line[:line.index(name)]
            if verb.search(line) or poss.search(before):
                return f"claims {name}, who is not on our roster"
    return None


def _describe_pick(p):
    """One pick, for the log line that records WHY we spoke.

    OUR OWN RANK IS PART OF IT. It told the room a sniped player was "right at
    the top of my list" and nothing in the transcript could confirm or deny
    that (2026-08-24). A claim about our own board should be checkable
    afterwards, and the number is already in hand.
    """
    out = f"pick {p.get('pick')} {p.get('by')} took {p.get('player')}"
    if p.get("we_wanted"):
        rank = p.get("our_rank_for_him")
        out += f" (WE WANTED HIM{f' #{rank}' if rank else ''})"
    if p.get("verdict"):
        out += f" [{p['verdict']}]"
    return out


def _worth_most(picks):
    """The one pick to react to when several qualify at once.

    A SNIPED TARGET OUTRANKS A GOOD VERDICT. Taking the most recent instead
    picked the wrong one live: pick 42 took a player off our shortlist and pick
    43 was somebody else's steal, and it remarked on the steal (2026-08-24).
    The brief already says being sniped is the most quotable thing that
    happens in a draft; the code was handing it the other one.

    AND AMONG SNIPES, OUR RANKING DECIDES -- not recency. Losing the player we
    had third is a bigger loss than losing the one we had fifth, and reacting
    to the fifth while the third goes unmentioned reads as not really having
    had a board. Observed once the rank was visible in the log: picks 32 and 33
    took our #3 and our #5, and it mourned the #5 (2026-08-25).

    A snipe with no rank recorded sorts last among snipes but still beats a
    verdict: we know we wanted him, we just cannot say how much.
    """
    if not picks:
        return None
    sniped = [p for p in picks if p.get("we_wanted")]
    if sniped:
        return min(sniped,
                   key=lambda p: (p.get("our_rank_for_him") or 10 ** 6))
    return picks[-1]


def compose(messages, context=None, reacting_to=None, _post_fn=None):
    """A line to post, or None to stay quiet.

    Fires on a chat message, on a notable PICK, or both. `reacting_to` is the
    pick that prompted it when nobody said anything -- the model is told
    plainly that it is speaking unprompted, because "reply to this" and
    "remark on that" want different lines."""
    if not messages and not reacting_to:
        return None
    payload = {
        "draft": context or {},
        "recent_chat": [{"from": m.get("author"), "said": m.get("text")}
                        for m in (messages or [])[-8:]],
    }
    if reacting_to:
        payload["reacting_to"] = reacting_to
        payload["unprompted"] = not messages
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
        # Pick numbers already considered for an unprompted reaction. Primed
        # on the first pass like `seen`, so arriving mid-draft does not produce
        # a burst of commentary about picks that happened before we got here.
        self.seen_picks = set()
        self.last_sent_at = 0.0
        self._now = _now or time.time
        self.primed = False

    def tick(self, context=None, _read_fn=None, _send_fn=None, _post_fn=None):
        """One pass. Returns (action, detail) for logging.

        action is one of: "quiet", "rate_limited", "would_say", "said",
        "dropped", "send_failed", "error"."""
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
        board = (context or {}).get("recent_picks") or []
        picks = notable_picks(board, self.seen_picks)
        # PRIME ON THE FIRST PASS. Everything already in the room when we
        # arrived is history, not something to answer; without this the bot
        # opens by replying to a conversation that finished an hour ago -- and,
        # now that picks are a trigger too, by remarking on half a draft.
        self.seen.update(m["text"] for m in msgs if m.get("text"))
        self.seen_picks.update(p.get("pick") for p in board
                               if p.get("pick") is not None)
        if not self.primed:
            self.primed = True
            return "quiet", (f"primed with {len(self.seen)} message(s), "
                             f"{len(self.seen_picks)} pick(s)")
        if not fresh and not picks:
            return "quiet", "nothing new"

        since = self._now() - self.last_sent_at
        if since < self.min_gap_s:
            return "rate_limited", f"{self.min_gap_s - since:.0f}s to go"

        # Carried into the return so the log shows the EXCHANGE. A
        # transcript of only our own lines reads like a bot talking to itself,
        # which is also the failure mode worth spotting early.
        if fresh:
            prompt = " | ".join(f"{m['author']}: {m['text'][:60]}"
                                for m in fresh[-2:])
        else:
            prompt = " | ".join(_describe_pick(p) for p in picks[-2:])
        # CHAT WINS when both fired. Somebody spoke to us and answering that
        # matters more than remarking on the board; the picks are in `context`
        # either way, so nothing is lost by letting the reply carry them.
        line = compose(msgs if fresh else None, context=context,
                       reacting_to=(None if fresh else _worth_most(picks)),
                       _post_fn=_post_fn)
        if not line:
            return "quiet", f"nothing worth saying (re: {prompt})"
        # THE LAST WORD IS A CHECK, NOT AN INSTRUCTION. Three paragraphs were
        # added to the brief telling the model not to invent things and each
        # failure recurred; every rule enforced in code has held. Dropped lines
        # are logged with the reason, so "made something up" never looks like
        # "had nothing to say".
        why = unverifiable(line, context)
        if why:
            return "dropped", f"{line}   <- UNVERIFIABLE: {why}"
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
