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
  * IT POSTS BY DEFAULT NOW (owner's call, 2026-08-28). This used to default
    to `propose` -- compose, log, say nothing -- on the reasoning that posting
    to other people should never be switched on by accident. What changed is
    that the accident is now much harder: the verifier rejects a line making a
    checkable false claim BEFORE it is sent, and four live mocks have shown
    what the thing actually says. `propose` is still there and is still the
    right mode for a room of strangers.

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
# Same env var as wes_server and sleeper.agent -- see the note in agent.py. A
# mismatched num_ctx makes every alternation between callers reload the model.
NUM_CTX = int(os.environ.get("WES_NUM_CTX", "16384"))

# Floor between messages, in seconds. THE ONLY VOLUME CONTROL, now that the
# prompt no longer asks for restraint -- picks arrive far faster than chat, so
# nothing else stands between the room and a running commentary.
#
# 60s, down from 180s (owner's call, 2026-08-28). Measured across four live
# mocks at 180s: 3-5 messages a draft, gaps of 200-340s, and the notable-pick
# filter -- not this floor -- was usually the binding constraint. So the floor
# had headroom. It is still the backstop if that filter ever loosens.
MIN_GAP_S = 60.0

# ...but a message that NAMES us gets a shorter one. Unprompted commentary and
# a direct question are different acts: the first is the bot choosing to speak,
# which is what a floor exists to ration, and the second is somebody waiting
# for an answer. Rationing the reply is how it comes across as broken rather
# than restrained -- asked "what happened bro" it once said nothing at all.
#
# Still a floor, not an exemption: someone spamming our name cannot turn this
# into a chat client.
DIRECT_GAP_S = 30.0

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
    "never disown it. Denying your own roster loses the room.\n"
    "AND THE MIRROR OF THAT: NEVER CLAIM A PLAYER YOU DID NOT DRAFT. "
    "`our_roster` is the COMPLETE list of who is yours; a pick with "
    "`ours: false` belongs to the manager in its `by` field and to nobody "
    "else. Being in `recent_picks` means somebody took him, not that you did. "
    "Everyone can see the board; a made-up claim is not a joke, it just makes "
    "you look like you are not watching.\n"
    "If your own player carries an injury flag, LEAD BY OWNING IT. \"Yes he's "
    "questionable, I took the upside\" is a position. Denying the tag is not, "
    "and everyone in the room can see it.\n"
    "Rules: one sentence, under 200 characters. No hashtags. No emoji spam "
    "(one is plenty). Never insult anyone's appearance, intelligence, family, "
    "or anything about who they are — the target is always their FANTASY "
    "TEAM. Keep it the kind of ribbing friends enjoy.\n"
    "`addressed_to_you` MEANS SOMEBODY IS WAITING. When it is present, those "
    "messages named you -- answer THEM first and directly, before any "
    "commentary about the board. A reply that ignores the question and "
    "remarks on a pick instead reads as not listening.\n"
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
    """One chat call, logged in full.

    The payload matters more here than anywhere: every fabrication this bot has
    produced was traced by hand afterwards, against an API, because the thing
    the model was looking at when it said it was never written down."""
    from sleeper import draft_log as wes_draft_log
    # keep_alive=-1 for the same reason as the pick call (see agent.py): an
    # omitted value resets the server's pin to Ollama's 5-minute default, so a
    # chat line between turns can be what evicts the model the next PICK needs.
    # num_predict: a banter line is one sentence -- measured 82-112 tokens --
    # and 128 keeps a chatty generation from running into the pick clock.
    body = json.dumps({
        "model": MODEL, "stream": False, "format": "json", "think": False,
        "keep_alive": -1,
        "options": {"temperature": 0.8,    # banter, not arithmetic
                    "num_predict": 128,
                    "num_ctx": NUM_CTX},
        "messages": [{"role": "system", "content": SYSTEM},
                     {"role": "user", "content": json.dumps(payload)}],
    }).encode()
    raw = None
    resp = None      # the whole Ollama reply, for its duration breakdown
    t0 = time.time()
    try:
        if _post_fn is not None:
            raw = _post_fn(body)
        else:
            req = urllib.request.Request(
                OLLAMA_URL + "/api/chat", data=body,
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=60) as r:
                resp = json.load(r)
            raw = resp["message"]["content"]
        got = json.loads(raw)
        wall = time.time() - t0
        wes_draft_log.log_call("draft.banter", payload, raw, wall, model=MODEL,
                               **wes_draft_log.timings(resp, wall))
        return got if isinstance(got, dict) else None
    except Exception as e:  # noqa: BLE001 — banter must never break a draft
        wall = time.time() - t0
        wes_draft_log.log_call("draft.banter", payload, raw, wall,
                               error=f"{type(e).__name__}: {e}", model=MODEL,
                               **wes_draft_log.timings(resp, wall))
        return None


def _name_forms(me):
    """The ways somebody might write our name, normalised.

    People address each other by DISPLAY name in this room -- the live logs
    have "GMSnappy,", "aykutb," and "awarmwalrus" -- but nobody types
    "GMBartimusPrime" twice. They shorten it. So the full name plus THE SINGLE
    LONGEST camel-case component, which for GMBartimusPrime is "bartimus".

    The longest, not every component over some length. Taking all of them at
    five-plus characters also accepted "prime", and "prime time baby" and
    "prime pick right there" are things people say about players -- both would
    have jumped the queue as if somebody had asked us a question. The longest
    component is the distinctive one by construction; the others are the
    prefixes and suffixes half the room shares."""
    flat = "".join(c for c in (me or "").lower() if c.isalnum())
    forms = {flat} if flat else set()
    parts = re.findall(r"[A-Z]?[a-z]+|\d+", me or "")
    if parts:
        longest = max(parts, key=len)
        if len(longest) >= 5:
            forms.add(longest.lower())
    return {f for f in forms if f}


def addressed_to(messages, me):
    """The messages that NAME us. Someone talking TO us, not near us.

    A question aimed at the room is not the same as one aimed at you, and
    answering the second slowly is what reads as broken -- asked "what happened
    bro" after losing a pick, this said nothing for a whole poll cycle
    (2026-08-21). Being named is the one signal that is checkable without
    guessing at intent."""
    forms = _name_forms(me)
    if not forms:
        return []
    out = []
    for m in messages or []:
        flat = "".join(c for c in (m.get("text") or "").lower()
                       if c.isalnum())
        if any(f in flat for f in forms):
            out.append(m)
    return out


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
# An overall pick number: "at 53", "at pick 53", "fell to pick 53", "with the
# 53rd". MORE THAN ONE PREPOSITION, because "at" alone was too narrow and let a
# real fabrication through: "Lamar Jackson just fell TO pick 23" -- he went at
# 26 (2026-08-28). The rule was right and only the phrasing escaped it, which
# is the cheapest kind of miss to fix and the easiest to not notice.
#
# The negative lookahead keeps it off projections and rates -- "255.2 points",
# "3.0 YPRR", "23% target share" are numbers a draft room says constantly and
# none of them are pick numbers.
_AT_PICK = re.compile(
    r"\b(?:at|to|with)\s+(?:the\s+)?(?:pick\s+)?(\d{1,3})(?:st|nd|rd|th)?\b"
    r"(?!\s*(?:%|percent|point|pt|yard|yd|target|catch|rec|snap|carr))",
    re.I)
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


def build_payload(messages, context, reacting_to=None, direct=None):
    """Exactly what the model is shown, in one place.

    SHARED WITH THE LOGGER ON PURPOSE. The outcome records used to log the
    draft `context` alone while the model was actually given context AND the
    chat -- so a dropped line was filed without the messages that provoked it,
    which is half its cause. Two constructions of "the payload" is one too
    many; if it drifts again the log stops describing the call it claims to."""
    payload = {
        "draft": context or {},
        "recent_chat": [{"from": m.get("author"), "said": m.get("text")}
                        for m in (messages or [])[-8:]],
    }
    if reacting_to:
        payload["reacting_to"] = reacting_to
        payload["unprompted"] = not messages
    # NAMED, so the reply should read as an answer rather than a remark. The
    # model cannot reliably spot this itself -- "GMBartimusPrime" and
    # "bartimus" and a bare question all look like chat to it -- and it is a
    # fact we already computed to pick the rate limit.
    if direct:
        payload["addressed_to_you"] = [
            {"from": m.get("author"), "said": m.get("text")} for m in direct]
    return payload


def compose(messages, context=None, reacting_to=None, _post_fn=None,
            direct=None):
    """A line to post, or None to stay quiet.

    Fires on a chat message, on a notable PICK, or both. `reacting_to` is the
    pick that prompted it when nobody said anything -- the model is told
    plainly that it is speaking unprompted, because "reply to this" and
    "remark on that" want different lines."""
    if not messages and not reacting_to:
        return None
    payload = build_payload(messages, context, reacting_to, direct)
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
                 min_gap_s=MIN_GAP_S, _now=None, browser=None,
                 direct_gap_s=DIRECT_GAP_S):
        # An optional held-open browser. Reading the chat cost a full launch
        # every poll -- ~9s to fetch a handful of messages -- which is what
        # made the bot feel sluggish in a live room.
        self.browser = browser
        # Defaulted late, from wes_sleeper, so switching accounts is one
        # setting rather than a hunt. Getting this wrong is not cosmetic: `me`
        # is how the bot knows not to answer itself, and a mismatch turns two
        # agents in a room into an infinite loop with an audience.
        from sleeper import data as wes_sleeper
        self.draft_id = draft_id
        self.me = me or wes_sleeper.USERNAME
        self.mode = mode
        self.min_gap_s = min_gap_s
        # Never longer than the general floor: a caller who widens the gap to
        # quieten the bot down must not accidentally make it chattier when
        # spoken to.
        self.direct_gap_s = min(direct_gap_s, min_gap_s)
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

        # SPOKEN TO, or merely speaking? A message that names us gets the
        # shorter floor: somebody is waiting on an answer, and rationing that
        # is what reads as broken rather than restrained.
        direct = addressed_to(fresh, self.me)
        gap = self.direct_gap_s if direct else self.min_gap_s
        since = self._now() - self.last_sent_at
        if since < gap:
            return "rate_limited", (f"{gap - since:.0f}s to go"
                                    f"{' (direct)' if direct else ''}")

        # Carried into the return so the log shows the EXCHANGE. A
        # transcript of only our own lines reads like a bot talking to itself,
        # which is also the failure mode worth spotting early.
        if fresh:
            prompt = " | ".join(f"{m['author']}: {m['text'][:60]}"
                                for m in fresh[-2:])
            if direct:
                prompt = "@us " + prompt
        else:
            prompt = " | ".join(_describe_pick(p) for p in picks[-2:])
        # CHAT WINS when both fired. Somebody spoke to us and answering that
        # matters more than remarking on the board; the picks are in `context`
        # either way, so nothing is lost by letting the reply carry them.
        line = compose(msgs if fresh else None, context=context,
                       reacting_to=(None if fresh else _worth_most(picks)),
                       _post_fn=_post_fn, direct=direct)
        if not line:
            return "quiet", f"nothing worth saying (re: {prompt})"
        # THE LAST WORD IS A CHECK, NOT AN INSTRUCTION. Three paragraphs were
        # added to the brief telling the model not to invent things and each
        # failure recurred; every rule enforced in code has held. Dropped lines
        # are logged with the reason, so "made something up" never looks like
        # "had nothing to say".
        # The payload as the model saw it -- context AND chat -- so the record
        # describes the call rather than half of it.
        shown = build_payload(msgs if fresh else None, context,
                              None if fresh else _worth_most(picks), direct)
        why = unverifiable(line, context)
        if why:
            return self._outcome("dropped", line, shown, prompt, error=why)
        if self.mode != "auto":
            self.last_sent_at = self._now()   # propose mode is rate-limited too
            return self._outcome("would_say", line, shown, prompt)
        try:
            ok = (_send_fn(self.draft_id, line) if _send_fn
                  else _default_send(self.draft_id, line, self.browser))
        except Exception as e:  # noqa: BLE001
            return "error", f"{type(e).__name__}: {e}"
        # The clock starts whether or not it landed: a failing send that is
        # retried every poll is exactly the flood this exists to prevent.
        self.last_sent_at = self._now()
        return self._outcome("said" if ok else "send_failed", line, shown,
                             prompt)

    def _outcome(self, action, line, shown, prompt, error=None):
        """Record what became of a composed line, and return (action, detail).

        EVERY TERMINAL PATH, not just the drops. The first cut logged only
        rejections, which answered "did it make something up" and left the more
        common question -- did this line actually reach the room -- with no
        record at all. A line that was composed and rate-limited, or composed
        in propose mode and never sent, looked from the log exactly like a line
        that was posted.

        The kind carries the action (draft.banter.said / .dropped /
        .would_say / .send_failed) so one filter separates them. `prompt` is
        what we were reacting to, which is the other half of judging the line:
        a dropped fabrication is only diagnosable next to the context that
        produced it."""
        detail = (f"{line}   <- UNVERIFIABLE: {error}" if error
                  else f"{line}   <- re: {prompt}")
        try:
            from sleeper import draft_log as wes_draft_log
            wes_draft_log.log_call(f"draft.banter.{action}", shown, line,
                                   error=error, reacting_to=prompt,
                                   mode=self.mode)
        except Exception:  # noqa: BLE001 — logging must never break a draft
            pass
        return action, detail


def _default_read(draft_id, browser=None):
    from sleeper import data as wes_sleeper
    return wes_sleeper.read_chat(draft_id, browser=browser)


def _default_send(draft_id, text, browser=None):
    from sleeper import data as wes_sleeper
    return wes_sleeper.send_chat(draft_id, text, browser=browser)
