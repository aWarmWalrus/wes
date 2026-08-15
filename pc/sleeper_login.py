r"""One-time interactive Sleeper login for the automation profile (#039).

Sleeper's public API is read-only, so any WRITE goes through the web app, which
means the persistent browser profile has to hold a real signed-in session. This
opens a VISIBLE browser and waits while the owner signs in by hand — the same
one-time pairing shape as the Yahoo profile.

    & C:\Users\awarm\wes-pc\.venv\Scripts\python.exe C:\Users\awarm\wes\pc\sleeper_login.py

Deliberately interactive and deliberately NOT automated: the credentials are the
owner's, they are stored nowhere in this repo, and a script that typed them
would have to hold them. The session cookie lands in the profile directory
(PC-local, never the repo) and persists across runs.

(Raw docstring so the Windows paths above stay literal — "\U" in a normal string
is a unicode escape and will not compile.)
"""
import sys
import time

sys.path.insert(0, r"C:\Users\awarm\wes\pc")
import wes_sleeper as sl  # noqa: E402

LEAGUE = "1393935116232818688"
WAIT_S = 300


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print(f"profile: {sl.PROFILE_DIR}")
    ok, detail = sl.logged_in(LEAGUE)
    if ok:
        print(f"already signed in — {detail}")
        return 0

    print("opening a visible browser; sign in, then leave it alone.")
    with sl._Session(headless=False) as page:
        page.goto(f"{sl.WEB}/leagues/{LEAGUE}/team",
                  wait_until="domcontentloaded", timeout=60000)
        deadline = time.time() + WAIT_S
        while time.time() < deadline:
            page.wait_for_timeout(3000)
            body = " ".join((page.inner_text("body") or "").split())
            if not sl._is_login_wall(page.url, body):
                print(f"signed in — team page loaded ({len(body)} chars)")
                return 0
            print(f"  waiting... ({int(deadline - time.time())}s left)",
                  flush=True)
    print("timed out, still on the login wall; rerun when ready")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
