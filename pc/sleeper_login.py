r"""Check the Sleeper automation session (#039).

    & C:\Users\awarm\wes-pc\.venv\Scripts\python.exe C:\Users\awarm\wes\pc\sleeper_login.py

THIS NO LONGER LOGS IN INTERACTIVELY, and that is deliberate. sleeper.com's
login form sits behind hCaptcha, which is built to detect precisely the browser
Playwright launches: the owner could not clear it by hand in the automation
profile, and even a success would not have lasted, because hCaptcha
re-challenges whenever it feels like it.

The captcha guards OBTAINING a session, not PRESENTING one. So the session comes
from an account token in `WES_SLEEPER_TOKEN` (PC user environment, never the
repo), injected into the page's localStorage under the key Sleeper's web app
actually reads. That walks straight past the captcha, and the app then
bootstraps its own session state as though a human had signed in.

This script just reports whether that works right now — the thing to run when
something else fails and you want to know if the session is the reason.

(Raw docstring so the Windows paths stay literal: "\U" in a normal string is a
unicode escape and will not compile.)
"""
import sys

sys.path.insert(0, r"C:\Users\awarm\wes\pc")
import wes_sleeper as sl  # noqa: E402

LEAGUE = "1393935116232818688"


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print(f"profile : {sl.PROFILE_DIR}")
    print(f"token   : {'set (%d chars)' % len(sl.TOKEN) if sl.TOKEN else 'MISSING'}")
    if not sl.TOKEN:
        print("\nSet WES_SLEEPER_TOKEN in the user environment, then re-run.")
        print("Note: a process only sees it after being restarted.")
        return 1

    ok, detail = sl.logged_in(LEAGUE)
    print(f"session : {'OK' if ok else 'NOT SIGNED IN'} — {detail}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
