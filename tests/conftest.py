"""Shared pytest config + fixtures for the PC-side tests.

The Pi-side vision tests live in test_faces.py and run under SYSTEM python3
(they need cv2/hailo); they are NOT collected by pytest here.
"""
import os
import subprocess
import sys
import tempfile
import urllib.request

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "pc"))  # so `import wes_server` works
sys.path.insert(0, REPO)  # so `import wes_hosts` (repo-root registry) works

SERVER_URL = os.environ.get("WES_TEST_URL", "http://127.0.0.1:8080")


def pytest_addoption(parser):
    parser.addoption(
        "--run-e2e", action="store_true", default=False,
        help="run @e2e tests (needs a running server + Claude API)",
    )


def pytest_collection_modifyitems(config, items):
    if config.getoption("--run-e2e"):
        return
    skip = pytest.mark.skip(reason="needs --run-e2e (running server + API)")
    for item in items:
        if "e2e" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(autouse=True)
def _sandbox_ledgers(tmp_path, monkeypatch):
    """Unit tests exercise routes that write the usage ledger and turn log —
    point both at tmp so test runs never pollute the real files."""
    import wes_server as ws

    monkeypatch.setattr(ws, "USAGE_LOG", str(tmp_path / "usage.csv"))
    monkeypatch.setattr(ws, "TURNS_LOG", str(tmp_path / "turns.jsonl"))
    monkeypatch.setattr(ws, "CONV_DIR", str(tmp_path / "conversations"))


@pytest.fixture(scope="session")
def server_url():
    return SERVER_URL


@pytest.fixture(scope="session")
def server_up():
    try:
        with urllib.request.urlopen(SERVER_URL + "/health", timeout=3) as r:
            return r.status == 200
    except Exception:  # noqa: BLE001
        return False


@pytest.fixture(scope="session")
def speech_wav():
    """Synthesize a known sentence with piper -> WAV bytes, for STT round-trips.

    Cached on disk so repeated runs don't re-synthesize."""
    import wes_server as ws

    text = "What time is it right now"
    path = os.path.join(tempfile.gettempdir(), "wes_test_speech.wav")
    if not os.path.exists(path):
        subprocess.run(
            [ws.PIPER_BIN, "-m", ws.VOICE_MODEL, "-f", path],
            input=text.encode(), check=True, capture_output=True,
        )
    with open(path, "rb") as f:
        return {"text": text, "wav": f.read()}
