"""Naming an account switches its credentials, not just its seat.

`--username` used to move only the name the seat lookups key on, while TOKEN
stayed whatever import time resolved — so naming a second account drafted with
the first one's credentials and said nothing about it. That is the failure
wes_sleeper's own header warns about, made reachable by a flag.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "pc"))

from sleeper import data as wes_sleeper  # noqa: E402
from sleeperdraft import config as sd_config  # noqa: E402


class TestUseAccount:
    def test_it_moves_the_username_and_the_token_together(self, monkeypatch):
        monkeypatch.setenv("WES_SLEEPER_TOKEN_BOTONE", "tok-one")
        monkeypatch.setenv("WES_SLEEPER_TOKEN_BOTTWO", "tok-two")
        assert wes_sleeper.use_account("botone")
        assert (wes_sleeper.USERNAME, wes_sleeper.TOKEN) == ("botone",
                                                             "tok-one")
        assert wes_sleeper.use_account("bottwo")
        assert (wes_sleeper.USERNAME, wes_sleeper.TOKEN) == ("bottwo",
                                                             "tok-two")

    def test_the_package_sees_the_switch_too(self, monkeypatch):
        """sleeperdraft reads config.USERNAME/TOKEN at call time, so both
        copies have to move or the write lands as the wrong person."""
        monkeypatch.setenv("WES_SLEEPER_TOKEN_BOTONE", "tok-one")
        wes_sleeper.use_account("botone")
        assert sd_config.USERNAME == "botone"
        assert sd_config.TOKEN == "tok-one"

    def test_a_per_account_token_beats_the_shared_one(self, monkeypatch):
        """Adding a bot account must never displace the account holding the
        real league team."""
        monkeypatch.setenv("WES_SLEEPER_TOKEN", "shared")
        monkeypatch.setenv("WES_SLEEPER_TOKEN_BOTONE", "mine")
        wes_sleeper.use_account("botone")
        assert wes_sleeper.TOKEN == "mine"

    def test_it_falls_back_to_the_shared_token(self, monkeypatch):
        monkeypatch.setenv("WES_SLEEPER_TOKEN", "shared")
        monkeypatch.delenv("WES_SLEEPER_TOKEN_NOBODY", raising=False)
        wes_sleeper.use_account("nobody")
        assert wes_sleeper.TOKEN == "shared"

    def test_an_account_with_no_token_reports_false(self, monkeypatch):
        """The caller stops rather than drafting anonymously — an anonymous
        browser scrapes the marketing page and reports an empty draft room."""
        monkeypatch.delenv("WES_SLEEPER_TOKEN", raising=False)
        monkeypatch.delenv("WES_SLEEPER_TOKEN_GHOST", raising=False)
        monkeypatch.setattr(sd_config, "ENV_PREFIXES", ["SLEEPER"])
        assert wes_sleeper.use_account("ghost") is False

    def test_it_says_when_the_token_came_from_the_shared_fallback(
            self, monkeypatch):
        """The fallback is a trap worth seeing: a typo'd username gets a
        perfectly valid token belonging to somebody else and reports success.
        `--username ghostaccount` came back "token: 357 chars" (2026-08-28)."""
        monkeypatch.setenv("WES_SLEEPER_TOKEN", "shared")
        monkeypatch.setenv("WES_SLEEPER_TOKEN_BOTONE", "mine")
        wes_sleeper.use_account("botone")
        assert wes_sleeper.TOKEN_SOURCE == "per-account"
        wes_sleeper.use_account("typo")
        assert wes_sleeper.TOKEN_SOURCE == "shared"

    def test_the_source_is_decided_by_NAME_not_by_value(self, monkeypatch):
        """The first cut compared the resolved token against the shared one,
        which cannot tell them apart when they hold the same string — exactly
        the live case, the shared variable being a copy of the bot's. It
        reported the bot's own account as running on a fallback."""
        monkeypatch.setenv("WES_SLEEPER_TOKEN", "same-string")
        monkeypatch.setenv("WES_SLEEPER_TOKEN_BOTONE", "same-string")
        wes_sleeper.use_account("botone")
        assert wes_sleeper.TOKEN_SOURCE == "per-account"

    def test_no_token_anywhere_reports_none(self, monkeypatch):
        monkeypatch.delenv("WES_SLEEPER_TOKEN", raising=False)
        monkeypatch.delenv("WES_SLEEPER_TOKEN_GHOST", raising=False)
        monkeypatch.setattr(sd_config, "ENV_PREFIXES", ["SLEEPER"])
        wes_sleeper.use_account("ghost")
        assert wes_sleeper.TOKEN_SOURCE == "none"

    def test_the_default_account_is_the_bot(self):
        """Set as the default so a bare draft command runs as the bot rather
        than the owner's real account."""
        import importlib
        src = importlib.util.find_spec("sleeper.data").origin
        with open(src, encoding="utf-8") as f:
            body = f.read()
        assert 'os.environ.get("WES_SLEEPER_USER", "gmbartimusprime")' in body
