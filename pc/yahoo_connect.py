"""One-time Yahoo Fantasy sign-in (ticket #029 P0).

The official Yahoo Fantasy API was abandoned 2026-07-17 (approval gate + a
no-caching DocuSign — see docs/fantasy-gm-design.md §1/§10). We drive the Yahoo
WEB UI in a persisted, logged-in browser session instead. This script does the
one-time sign-in; after it, the session lives in the browser profile and reads
run unattended until it expires.

    1. Install the browser automation once (PC venv):
         pip install playwright
         playwright install chromium

    2. Run:  python pc\yahoo_connect.py
       A browser window opens at Yahoo login. Sign in (including 2FA). When
       you're logged in, return to the console and press Enter. The session is
       saved to the profile dir (WES_YAHOO_PROFILE_DIR, default
       ~/wes-pc/yahoo_profile) — a SECRET; keep it off the repo and out of chat.

    3. Put the league_key / team_key you want to manage into your teams.yaml
       (see fantasy/teams.example.yaml). You can read these off the league URL.

There are NO client id/secret and NO API tokens anymore — the profile cookies
are the only credential.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import wes_yahoo as wy  # noqa: E402


def main():
    # Service consoles are cp1252: keep output ASCII-safe (docs/setup.md).
    if not wy._have_playwright():
        print("ERROR: Playwright isn't installed in this venv.")
        print("  pip install playwright")
        print("  playwright install chromium")
        print("then re-run this script.")
        return 1

    if wy.has_session():
        print(f"A session profile exists at {wy.PROFILE_DIR}; checking it...")
        if wy.logged_in():
            print("Still signed in.")
            print(wy.my_teams())
            return 0
        print("...but it's signed out (login never completed or expired).")
        print("Re-opening the browser to sign in again.\n")

    print("Opening a browser for you to sign in to Yahoo...")
    ok = wy.login()
    if not ok:
        print("Sign-in did not complete.")
        return 1
    if not wy.logged_in():
        print("WARNING: still not signed in after login. Make sure you fully")
        print("completed Yahoo sign-in (including 2FA) BEFORE pressing Enter.")
        return 1

    print("\nConnected. Now copy the league_key / team_key you want to manage")
    print("into your teams.yaml (see fantasy/teams.example.yaml).")
    print("\nQuick check of your teams:")
    print(wy.my_teams())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
