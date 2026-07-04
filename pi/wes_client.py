"""WES Tier 1 client (runs on the Raspberry Pi).

Listens for the wake word with openwakeword, records the following utterance,
and POSTs it to the Tier 2 PC service, which handles STT -> Claude -> TTS ->
casting the spoken reply to the Kitchen Display. The Pi just prints the
transcript and reply for logging.

Wake-word detection and audio capture stay on the Pi; everything heavier runs
on the PC (10.0.0.168).

Run on the Pi:
    ~/wes/.venv/bin/python ~/wes/pi/wes_client.py
"""

import numpy as np
import pyaudio
import requests
import base64
import csv
import io
import json
import os
import random
import subprocess
import tempfile
import threading
import time
import wave
from datetime import datetime
from urllib.parse import quote, unquote

from openwakeword.model import Model
from openwakeword.vad import VAD  # Silero VAD — speech-vs-noise for end-of-speech

import pi_state  # read-only Pi state endpoint for Claude tools

# --- Config ----------------------------------------------------------------

PC_URL = "http://10.0.0.168:8080/respond"  # non-streaming (returns full WAV)
HEALTH_URL = "http://10.0.0.168:8080/health"  # server readiness probe
STREAM_URL = "http://10.0.0.168:8080/respond_stream"  # streams raw PCM as generated
SPECULATE_URL = "http://10.0.0.168:8080/speculate"  # partial-audio prefetch
SPECULATE_START_S = 5.0     # don't speculate until the utterance is this long
SPECULATE_INTERVAL_S = 3.0  # then send a partial snapshot this often
PREFETCH_SCENE_URL = "http://10.0.0.168:8080/prefetch_scene"  # wake-word vision prefetch
CAPTURE_FRAME_PY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "capture_frame.py")
HAILO_FACES_PY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hailo_faces.py")

MIC_DEVICE_INDEX = 0       # HD Pro Webcam C920: USB Audio
SAMPLE_RATE = 16000
CHUNK = 1280               # ~80ms at 16kHz, openwakeword's frame size
THRESHOLD = 0.5
# "hey_jarvis" is the only bundled openwakeword model; a custom "hey_wesley"
# would need training (see CLAUDE.md).
WAKE_WORD = "hey_jarvis"

SILENCE_SECONDS = 0.8      # no-speech duration before we stop recording
# End-of-speech via Silero VAD (speech probability), robust to background noise
# where energy thresholding fails. Speech >~0.5; noise/silence well below.
VAD_THRESHOLD = float(os.environ.get("WES_VAD_THRESHOLD", "0.5"))
VAD_FRAME_SIZE = 640  # must divide CHUNK (1280 = 2*640); 480 does NOT divide 1280
SPEECH_START_TIMEOUT_S = 5.0  # wait this long for speech to begin before giving up
MAX_SENTENCE_SECONDS = 20  # hard cap on a single utterance

# C920 indicator LED as a status light. LED1 Mode via uvcdynctrl:
#   0=Off, 1=On, 2=Blink, 3=Auto — controllable without streaming the camera.
# Used as: On = "Wes heard you" (during a turn), Blink = Bluetooth speaker down.
CAMERA_DEV = "/dev/video0"

# Bluetooth speaker (JBL Flip 5) — monitored/kept connected by the Pi.
JBL_MAC = "28:FA:19:2F:0F:76"
JBL_SINK = "bluez_output.28_FA_19_2F_0F_76.1"
BT_CHECK_INTERVAL_S = 5.0


def log(msg):
    """Timestamped log line (ms) so it aligns with journalctl/wireplumber logs."""
    print(f"{datetime.now().strftime('%H:%M:%S.%f')[:-3]} {msg}", flush=True)


def set_led(mode):
    """Set the webcam indicator LED (0=Off, 1=On, 2=Blink, 3=Auto)."""
    try:
        subprocess.run(
            ["uvcdynctrl", "-d", CAMERA_DEV, "-s", "LED1 Mode", str(mode)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except FileNotFoundError:
        pass


def bt_connected():
    """True if the JBL is currently connected."""
    try:
        out = subprocess.run(
            ["bluetoothctl", "info", JBL_MAC],
            capture_output=True, text=True, timeout=10,
        ).stdout
        return "Connected: yes" in out
    except Exception:  # noqa: BLE001
        return False


def start_silence():
    """Permanent silent stream that holds the A2DP transport open, so per-turn
    playback never re-acquires it (prevents the BlueZ 'NotAuthorized' wedge)."""
    try:
        return subprocess.Popen(
            ["pacat", "--playback", "--raw", "--rate=22050", "--format=s16le",
             "--channels=1", "--device=" + JBL_SINK, "/dev/zero"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        return None


def bt_monitor(state):
    """Background loop: keep the JBL connected; keep a persistent silent stream
    holding the A2DP transport; blink the LED while disconnected.

    While connected, the turn logic owns the LED. On a drop, force blink and
    reconnect; on reconnect, restore the default sink, clear the LED, and
    (re)start the persistent silence.
    """
    was = None
    while True:
        conn = bt_connected()
        state["bt"] = conn
        log(f"[bt] check: {'connected' if conn else 'DISCONNECTED'}")
        if not conn:
            set_led(2)  # blink = speaker disconnected
            state["silence"] = None  # the silent stream died with the sink
            log("[bt] reconnecting (bluetoothctl connect)...")
            try:
                out = subprocess.run(
                    ["bluetoothctl", "connect", JBL_MAC],
                    capture_output=True, text=True, timeout=15,
                )
                log(f"[bt] connect rc={out.returncode}")
            except Exception as e:  # noqa: BLE001
                log(f"[bt] connect error: {e!r}")
        else:
            if was is False:  # just came back
                r = subprocess.run(
                    ["pactl", "set-default-sink", JBL_SINK],
                    capture_output=True, text=True, check=False,
                )
                log(f"[bt] reconnected; set-default-sink rc={r.returncode}")
                set_led(0)
            # Ensure the persistent silence is running (holds the transport).
            sp = state.get("silence")
            if sp is None or sp.poll() is not None:
                state["silence"] = start_silence()
                if state["silence"]:
                    log("[audio] persistent silence started (holds A2DP transport)")
        was = conn
        time.sleep(BT_CHECK_INTERVAL_S)
TIMING_LOG = os.path.expanduser("~/wes/logs/timing.csv")

# Per-turn phases logged to TIMING_LOG (all durations in ms), streaming pipeline.
#   record_ms   — recording length (incl. end-of-speech silence)
#   stt_ms      — server STT (from response header)
#   headers_ms  — request start -> response headers (≈ STT + transfer)
#   ttfa_ms     — request start -> first reply audio byte (time-to-first-audio)
#   done_ms     — request start -> last reply audio byte
#   filler_ms   — filler playback length
#   gap_to_reply_ms — end-of-speech -> first reply audio (perceived latency)
#   reply_play_ms   — reply playback length (done - ttfa)
TIMING_FIELDS = [
    "ts", "spec", "record_ms", "stt_ms", "headers_ms", "ttfa_ms", "done_ms",
    "filler_ms", "gap_to_reply_ms", "reply_play_ms", "transcript",
]


def log_timing(row):
    """Append one turn's timing to the CSV (writing the header if new)."""
    os.makedirs(os.path.dirname(TIMING_LOG), exist_ok=True)
    new = not os.path.exists(TIMING_LOG)
    with open(TIMING_LOG, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=TIMING_FIELDS)
        if new:
            w.writeheader()
        w.writerow(row)


def wait_for_server():
    """Block until the PC server answers /health. Called before the ready state
    so we never listen for the wake word while turns are doomed to fail (the
    server warm-loads models for ~30-60s after it starts)."""
    attempt = 0
    while True:
        try:
            if requests.get(HEALTH_URL, timeout=3).ok:
                log("[startup] PC server is up")
                return
        except requests.RequestException:
            pass
        if attempt % 10 == 0:  # log every ~30s, not every retry
            log(f"[startup] waiting for PC server ({HEALTH_URL})...")
        attempt += 1
        time.sleep(3)


def prefetch_scene():
    """Capture a frame + recognize faces on the Hailo (system python3), then send
    the frame AND the identities to the PC so Gemma can describe the scene knowing
    who's who. All hidden behind the user's speech."""
    try:
        r = subprocess.run(
            ["/usr/bin/python3", HAILO_FACES_PY, "scene"],
            capture_output=True, text=True, timeout=20,
        )
        if r.returncode != 0 or not r.stdout:
            return
        data = json.loads(r.stdout)
        jpeg = base64.b64decode(data["jpeg_b64"]) if data.get("jpeg_b64") else b""
        if not jpeg:
            return
        faces = data.get("faces", [])
        requests.post(
            PREFETCH_SCENE_URL, data=jpeg,
            headers={
                "Content-Type": "image/jpeg",
                "X-Identities": quote(json.dumps(faces)),
            },
            timeout=10,
        )
        log(f"[vision] prefetched frame + identities {[f['name'] for f in faces]}")
    except Exception:  # noqa: BLE001
        pass


def send_speculation(samples):
    """Fire the audio-so-far to the PC to prefetch STT + a speculative reply."""
    secs = round(len(samples) / SAMPLE_RATE, 1)
    try:
        r = requests.post(
            SPECULATE_URL,
            data=to_wav_bytes(samples),
            headers={"Content-Type": "audio/wav"},
            timeout=10,
        )
        transcript = r.json().get("transcript", "")
        log(f"[speculate @{secs}s] {transcript!r}")
    except (requests.RequestException, ValueError):
        pass


def play_turn(wav, result):
    """Play one whole turn through a SINGLE paplay --raw stream: the streamed reply,
    with silence bridging the STT gap. One A2DP transport acquisition per turn —
    avoids the PipeWire 'NotAuthorized' churn/stalls from opening several players."""
    import queue

    q = queue.Queue()
    done = object()
    t_req0 = time.perf_counter()

    def fetch():
        try:
            r = requests.post(
                STREAM_URL, data=wav,
                headers={"Content-Type": "audio/wav"}, timeout=60, stream=True,
            )
            r.raise_for_status()
            result["headers_ms"] = (time.perf_counter() - t_req0) * 1000
            result["stt_ms"] = int(r.headers.get("X-Stt-Ms", 0) or 0)
            result["spec"] = r.headers.get("X-Spec", "?")
            result["transcript"] = unquote(r.headers.get("X-Transcript", ""))
            for chunk in r.iter_content(chunk_size=4096):
                if chunk:
                    q.put(chunk)
        except requests.RequestException as e:
            result["err"] = e
        finally:
            q.put(done)

    threading.Thread(target=fetch, daemon=True).start()

    player = subprocess.Popen(
        ["paplay", "--raw", "--rate=22050", "--format=s16le", "--channels=1"],
        stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    log("[audio] turn player started")
    # Stall watchdog: kill the player only after 30s with NO new reply data
    # (server hang, dead sink). The deadline resets on every received chunk, so
    # a long reply that is streaming fine is never cut off mid-sentence.
    stalled = threading.Event()
    last_data = [time.monotonic()]

    def stall_watch():
        while player.poll() is None:
            if time.monotonic() - last_data[0] > 30.0:
                stalled.set()
                player.kill()
                return
            time.sleep(1.0)

    threading.Thread(target=stall_watch, daemon=True).start()

    def play_error():
        return "server stalled >30s (watchdog)" if stalled.is_set() else "sink drop"

    silence = b"\x00" * 2048  # ~0.05s of silence, written to keep the sink fed
    first_reply = True
    try:
        while True:
            try:
                item = q.get(timeout=0.05)
            except queue.Empty:
                try:
                    player.stdin.write(silence)  # bridge the STT gap, no re-acquire
                except (BrokenPipeError, OSError):
                    result["play_error"] = play_error()
                    break
                continue
            last_data[0] = time.monotonic()  # data arrived — reset the stall timer
            if item is done:
                break
            if first_reply:
                result["ttfa_ms"] = (time.perf_counter() - t_req0) * 1000
                first_reply = False
            try:
                player.stdin.write(item)
            except (BrokenPipeError, OSError):
                result["play_error"] = play_error()
                break
        try:
            player.stdin.close()
        except (BrokenPipeError, OSError):
            pass
        player.wait()
    except Exception as e:  # noqa: BLE001
        result["err"] = e
    result["done_ms"] = (time.perf_counter() - t_req0) * 1000


def record_sentence(stream, vad):
    nonspeech_chunks = 0
    max_silent = int(SILENCE_SECONDS * SAMPLE_RATE / CHUNK)
    max_chunks = int(MAX_SENTENCE_SECONDS * SAMPLE_RATE / CHUNK)
    start_deadline = int(SPEECH_START_TIMEOUT_S * SAMPLE_RATE / CHUNK)
    spec_start = int(SPECULATE_START_S * SAMPLE_RATE / CHUNK)
    spec_every = max(1, int(SPECULATE_INTERVAL_S * SAMPLE_RATE / CHUNK))
    per_sec = max(1, int(SAMPLE_RATE / CHUNK))
    frames = []
    ended = "max"
    speech_started = False
    max_prob = 0.0
    vad.reset_states()

    print("  Listening for sentence...")
    for i in range(max_chunks):
        chunk = np.frombuffer(
            stream.read(CHUNK, exception_on_overflow=False), dtype=np.int16
        )
        frames.append(chunk)
        prob = float(vad.predict(chunk, frame_size=VAD_FRAME_SIZE))  # noise-robust
        max_prob = max(max_prob, prob)
        if prob >= VAD_THRESHOLD:
            if not speech_started:
                log(f"[rec] speech started at "
                    f"{round((i + 1) * CHUNK / SAMPLE_RATE, 1)}s (prob={prob:.2f})")
            speech_started = True
            nonspeech_chunks = 0
        else:
            nonspeech_chunks += 1
            # End only after speech began; before that, wait up to the start timeout.
            if speech_started and nonspeech_chunks >= max_silent:
                ended = "end-of-speech"
                break
            if not speech_started and (i + 1) >= start_deadline:
                ended = "no-speech-timeout"
                break
        if (i + 1) % per_sec == 0:
            log(f"[rec] {round((i + 1) * CHUNK / SAMPLE_RATE, 1)}s prob={prob:.2f} "
                f"started={speech_started} quiet={nonspeech_chunks}")
        # Prefetch STT + a speculative reply: start after ~5s, then every ~3s.
        n = i + 1
        if n >= spec_start and (n - spec_start) % spec_every == 0:
            snap = np.concatenate(frames).copy()
            threading.Thread(
                target=send_speculation, args=(snap,), daemon=True
            ).start()

    log(f"[rec] ended={ended} dur={round(len(frames) * CHUNK / SAMPLE_RATE, 1)}s "
        f"max_prob={max_prob:.2f}")
    return np.concatenate(frames)


def to_wav_bytes(samples_int16):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)  # int16
        w.setframerate(SAMPLE_RATE)
        w.writeframes(samples_int16.tobytes())
    return buf.getvalue()


def main():
    print("Loading wake-word model...")
    oww = Model(wakeword_models=[WAKE_WORD], inference_framework="onnx")
    vad = VAD()  # Silero VAD for noise-robust end-of-speech detection

    pa = pyaudio.PyAudio()
    # PulseAudio can transiently time out opening the capture stream (e.g. if a
    # previous run hasn't released the mic yet). Retry a few times before giving up.
    stream = None
    import time

    for attempt in range(1, 6):
        try:
            stream = pa.open(
                rate=SAMPLE_RATE,
                channels=1,
                format=pyaudio.paInt16,
                input=True,
                input_device_index=MIC_DEVICE_INDEX,
                frames_per_buffer=CHUNK,
            )
            break
        except OSError as e:
            print(f"  mic open failed (attempt {attempt}/5): {e}")
            time.sleep(2)
    if stream is None:
        print(
            "Could not open the mic. Is another wes_client already running?\n"
            "  Check: pgrep -af '[w]es_client'   Kill: pkill -9 -f wes_client.py"
        )
        pa.terminate()
        return

    set_led(0)  # start with the indicator off

    # Monitor the Bluetooth speaker: reconnect if it drops, blink the LED while down,
    # and hold the A2DP transport open with a persistent silent stream.
    state = {"bt": bt_connected(), "silence": None}
    threading.Thread(target=bt_monitor, args=(state,), daemon=True).start()

    # Read-only state endpoint the PC's Claude tools call for live Pi status.
    pi_state.start_state_server()
    log(f"[state] endpoint on :{pi_state.STATE_PORT}")

    # Don't enter the ready state until the PC server is actually reachable.
    wait_for_server()

    print(f'Listening for "Hey Jarvis"... (Ctrl+C to quit) -> {PC_URL}')
    cooldown = 0
    try:
        while True:
            audio = np.frombuffer(
                stream.read(CHUNK, exception_on_overflow=False), dtype=np.int16
            )
            score = oww.predict(audio).get(WAKE_WORD, 0)

            if cooldown > 0:
                cooldown -= 1
            elif score >= THRESHOLD:
                log(f"[wake] detected (score={score:.2f})")
                if not state["bt"]:
                    # No speaker — don't play to a dead sink. LED is already
                    # blinking (bt_monitor); just skip this turn.
                    log("[wake] speaker disconnected — skipping (LED blinking)")
                    cooldown = 20
                    continue
                set_led(1)  # light the indicator: "Wes heard you"
                # Snap a frame and start describing it now, so if this turn asks
                # "what do you see" the description is already cached.
                threading.Thread(target=prefetch_scene, daemon=True).start()
                try:
                    t_rec0 = time.perf_counter()
                    samples = record_sentence(stream, vad)
                    t_rec1 = time.perf_counter()  # end of speech
                    wav = to_wav_bytes(samples)

                    # One paplay stream for the whole turn (streamed reply,
                    # silence-bridged) — single A2DP acquisition, no sink thrashing.
                    result = {}
                    play_turn(wav, result)

                    if "err" in result:
                        log(f"[turn] request failed: {result['err']}")
                    else:
                        transcript = result.get("transcript", "")
                        ttfa = result.get("ttfa_ms", 0.0)
                        done = result.get("done_ms", ttfa)
                        log(f"[turn] transcript: {transcript!r}")
                        if result.get("play_error"):
                            log(f"[turn] playback interrupted: {result['play_error']}")
                        row = {
                            "ts": datetime.now().isoformat(timespec="seconds"),
                            "spec": result.get("spec", "?"),
                            "record_ms": round((t_rec1 - t_rec0) * 1000),
                            "stt_ms": result.get("stt_ms", 0),
                            "headers_ms": round(result.get("headers_ms", 0.0)),
                            "ttfa_ms": round(ttfa),
                            "done_ms": round(done),
                            "filler_ms": 0,  # filler is part of the single stream now
                            "gap_to_reply_ms": round(ttfa),  # ~ end-of-speech -> first audio
                            "reply_play_ms": round(done - ttfa),
                            "transcript": transcript,
                        }
                        log_timing(row)
                        log("[timing] " + " ".join(
                            f"{k}={row[k]}" for k in (
                                "spec", "record_ms", "stt_ms", "headers_ms",
                                "ttfa_ms", "done_ms", "gap_to_reply_ms", "reply_play_ms",
                            )
                        ))
                finally:
                    set_led(0 if state["bt"] else 2)
                cooldown = 20
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        set_led(0)  # ensure the indicator is off on exit
        stream.stop_stream()
        stream.close()
        pa.terminate()


if __name__ == "__main__":
    main()
