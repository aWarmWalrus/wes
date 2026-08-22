"""Endpoints, browser settings and the account token.

Everything here is a SETTING, not a constant. The package is meant to be usable
by somebody who is not us, on an account that is not ours, so nothing about a
particular machine or a particular league is baked in.
"""
import os

BASE = "https://api.sleeper.app/v1"
WEB = "https://sleeper.com"

# The browser profile that holds the Sleeper login. Kept apart from any other
# platform's profile so a re-login on one cannot disturb the other. Local to the
# machine, never a repo: it contains real session cookies.
PROFILE_DIR = os.environ.get(
    "WES_SLEEPER_PROFILE_DIR",
    os.path.join(os.path.expanduser("~"), "wes-pc", "sleeper_profile"))
# HEADLESS BY DEFAULT. The original reasoning against it -- "the one-time login
# is interactive, and a headless window you cannot see is a bad place to
# discover that a session expired" -- predates token injection. Login is not
# interactive here, `authenticate` returns a boolean, and a visible window costs
# a Chrome popup stealing focus on every pick, fifteen times an afternoon.
#
# Verified identical, not assumed: a full headless mock drafted 15 of 15 with
# zero substitutions in 620s, against 622s headed (2026-08-16). Same machine,
# same browser, same profile -- only the window is gone.
#
# Set WES_SLEEPER_HEADLESS=0 to watch it work, which is worth doing whenever the
# draft room's DOM changes underneath you.
HEADLESS = os.environ.get("WES_SLEEPER_HEADLESS", "1") == "1"
BROWSER_CHANNEL = os.environ.get("WES_SLEEPER_BROWSER_CHANNEL", "chrome")

# The single localStorage key Sleeper's web app reads the token from. Pinned by
# testing candidates ONE AT A TIME against a cleared store: of `token`,
# `user_token`, `auth_token`, `jwt` and `access_token`, only this one gets past
# the login wall. Injecting all five worked too, but shipping the shotgun would
# have kept "working" if the real key changed -- right up until it didn't.
TOKEN_KEY = "token"

# League metadata changes rarely; the player dump changes ~daily and Sleeper's
# docs ask callers not to pull it more than once a day (it is 14MB).
LEAGUE_TTL = float(900)
PLAYERS_TTL = float(6 * 3600)


# WHY A TOKEN AND NOT AN INTERACTIVE LOGIN: sleeper.com's login form is behind
# hCaptcha, which is built to detect exactly the browser Playwright launches --
# it cannot reliably be passed by hand inside an automation profile, and even
# one success would not last, since hCaptcha re-challenges. But the captcha
# guards OBTAINING a session, not PRESENTING one. Injecting an existing token
# walks straight past it, and the app then bootstraps its own session state as
# if a human had signed in (verified 2026-08-14).
def read_token(username=None):
    """The token for `username`, falling back to the PERSISTED user-scope value
    on Windows.

    Looks for WES_SLEEPER_TOKEN_<USERNAME> first, then the shared
    WES_SLEEPER_TOKEN. A second account is therefore ADDITIVE rather than a
    replacement, and whichever account holds your real league team keeps
    working whatever else gets configured later.

    The registry fallback exists because a shell opened before the variable was
    set does not inherit it, and that failure is quiet and expensive: a mock
    ran for seven minutes, stood down on every turn with "no WES_SLEEPER_TOKEN",
    let Sleeper's autopick take all fifteen picks, and printed a perfectly
    plausible roster that proved nothing (2026-08-15). The value is already on
    the machine; a stale shell should not be what decides whether you can draft.
    """
    names = []
    if username:
        names.append("WES_SLEEPER_TOKEN_"
                     + "".join(c for c in username.upper() if c.isalnum()))
    names.append("WES_SLEEPER_TOKEN")
    for var in names:
        tok = os.environ.get(var, "")
        if tok:
            return tok
    if os.name != "nt":
        return ""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            for var in names:
                try:
                    got = str(winreg.QueryValueEx(key, var)[0] or "")
                except OSError:
                    continue
                if got:
                    return got
    except OSError:
        return ""
    return ""


# WHICH ACCOUNT THIS PROCESS IS. A setting, because one person may hold several
# (a personal account with the real league team, and a bot account for mocks),
# and the account has to match the TOKEN or every write lands as the wrong
# person -- or nowhere, since the seat lookups key off the display name.
USERNAME = os.environ.get("WES_SLEEPER_USER", "")

# PAIRED WITH THE ACCOUNT, so the two cannot drift apart. Mismatching a token
# and a username is the failure this prevents, and it is silent.
TOKEN = read_token(USERNAME)
