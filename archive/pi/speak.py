"""WES audio output: synthesize text with piper TTS and cast it to a Google
Home / Nest speaker via catt.

Cast devices play media from a URL, so catt spins up a short-lived local HTTP
server to serve the generated WAV and tells the speaker to fetch+play it.

Default target is "Kitchen Display". Per project rule, "Good gray" and "Matcha"
are off-limits — do not set CAST_DEVICE to those.

Usage:
    python speak.py "Text for Wesley to say"
"""

import os
import subprocess
import sys
import tempfile

HOME = os.path.expanduser("~")

# Tooling installed on the Pi (see CLAUDE.md "Audio Output: Google Cast").
PIPER_BIN = os.path.join(HOME, "cast-venv", "bin", "piper")
CATT_BIN = os.path.join(HOME, "cast-venv", "bin", "catt")
VOICE_MODEL = os.path.join(HOME, "wes", "voices", "en_US-amy-medium.onnx")

# Target speaker. Kitchen Display is the approved response device.
CAST_DEVICE = "Kitchen Display"

# Playback volume as a fraction of max (0.0–1.0). 0.5 = 5/10.
# catt's volume scale is 0–100, so this is multiplied by 100.
CAST_VOLUME = 0.5


def synthesize(text, out_path):
    """Render `text` to a WAV file at `out_path` using piper."""
    subprocess.run(
        [PIPER_BIN, "-m", VOICE_MODEL, "-f", out_path],
        input=text.encode("utf-8"),
        check=True,
    )
    return out_path


def set_volume(device=CAST_DEVICE, level=CAST_VOLUME):
    """Set the device volume. `level` is a 0.0–1.0 fraction; catt wants 0–100."""
    subprocess.run(
        [CATT_BIN, "-d", device, "volume", str(int(round(level * 100)))],
        check=True,
    )


def cast(wav_path, device=CAST_DEVICE):
    """Cast a local audio file to the named Cast device (catt serves it)."""
    subprocess.run([CATT_BIN, "-d", device, "cast", wav_path], check=True)


def speak(text, device=CAST_DEVICE, volume=CAST_VOLUME):
    """Synthesize `text`, set volume, and cast it to `device`."""
    fd, wav_path = tempfile.mkstemp(prefix="wes_", suffix=".wav")
    os.close(fd)
    try:
        synthesize(text, wav_path)
        set_volume(device, volume)
        cast(wav_path, device)
    finally:
        try:
            os.remove(wav_path)
        except OSError:
            pass


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print('Usage: python speak.py "text to say"', file=sys.stderr)
        sys.exit(1)
    speak(" ".join(sys.argv[1:]))
