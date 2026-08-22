"""The held-open draft browser — now `sleeperdraft.browser`.

Moved out with the rest of the DOM layer so it can be used without WES. Kept as
a name here because the draft loop, the banter loop and the tests all import
it, and a rename buys nothing.
"""
from sleeperdraft.browser import MAX_AGE_S, MAX_USES, Browser

__all__ = ["Browser", "MAX_AGE_S", "MAX_USES"]
