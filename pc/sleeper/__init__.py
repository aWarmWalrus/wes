"""Everything WES knows about Sleeper: the data layer, the draft, the chat.

WHY A PACKAGE. `pc/` is flat and was up to twenty-seven modules, of which ten
were Sleeper. Reading any one of them meant scrolling past the rest, and the
`wes_` prefix said which project a file belonged to while saying nothing about
which SUBSYSTEM. Here the directory is the answer.

    data          the Sleeper API and the valued draft board
    draft_day     the entry point: pre-flight, wait for the room, hand off
    draft_run     the loop -- watch the clock, decide, pick, verify
    agent         the model call that chooses a player
    banter        draft-room chat: composing, the rate limits, the verifier
    chat_context  the payload the banter agent reasons over
    shortlist     what we want now, shared by the picker and the chat
    draft_log     what every model call was asked and answered
    reporting     ledger rows and the roster heartbeat line
    login         one-off session setup

IMPORTS ARE ABSOLUTE (`from sleeper import data`), not relative. `draft_day` is
run as `python -m sleeper.draft_day`, and absolute names behave the same there,
under pytest, and from a REPL -- relative ones do not survive being run as a
script, which is exactly how draft day starts.

The external `sleeperdraft` package is a different thing entirely: it owns the
browser and the DOM, knows nothing about WES, and is imported by `data` alone.
"""
