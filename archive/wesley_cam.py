import numpy as np
import pyaudio
import cv2
import os
import subprocess
import smtplib
import threading
from email.message import EmailMessage
from datetime import datetime
from faster_whisper import WhisperModel
from openwakeword.model import Model

PHOTOS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "photos")
CAMERA_INDEX = 0
MIC_DEVICE_INDEX = 0  # HD Pro Webcam C920: USB Audio (hw:2,0)
SAMPLE_RATE = 16000
CHUNK = 1280           # ~80ms at 16kHz, openwakeword's expected frame size
THRESHOLD = 0.5
WHITE_BALANCE = None   # Kelvin (2000–6500), or None for auto
BRIGHTNESS = 180       # v4l2 brightness (0–255, default 128); higher = brighter
WARMUP_FRAMES = 20     # frames to discard so auto-exposure can settle

# Read from the environment — NEVER hardcode credentials in source.
# The previously committed app password was leaked when this repo went public
# and MUST be revoked at https://myaccount.google.com/apppasswords
EMAIL_TO = os.environ.get("WES_CAM_EMAIL_TO", "")
EMAIL_FROM = os.environ.get("WES_CAM_EMAIL_FROM", "")
EMAIL_APP_PASSWORD = os.environ.get("WES_CAM_EMAIL_APP_PASSWORD", "")

SILENCE_SECONDS = 1.5
SILENCE_ENERGY = 300
MAX_SENTENCE_SECONDS = 10

_CLAHE = None


def apply_clahe(frame):
    global _CLAHE
    if _CLAHE is None:
        # clipLimit controls how aggressively local contrast is boosted;
        # tileGridSize controls the region size for local adaptation
        _CLAHE = cv2.createCLAHE(clipLimit=2.5, tileGridSize=(8, 8))
    lab = cv2.cvtColor(frame, cv2.COLOR_BGR2LAB)
    l, a, b = cv2.split(lab)
    l = _CLAHE.apply(l)
    return cv2.cvtColor(cv2.merge([l, a, b]), cv2.COLOR_LAB2BGR)


def set_camera_controls():
    cmds = []
    if WHITE_BALANCE is None:
        cmds.append("white_balance_automatic=1")
    else:
        cmds.append("white_balance_automatic=0")
        cmds.append(f"white_balance_temperature={int(WHITE_BALANCE)}")
    cmds.append(f"brightness={int(BRIGHTNESS)}")
    cmds.append("backlight_compensation=1")
    for ctrl in cmds:
        subprocess.run(["v4l2-ctl", f"--set-ctrl={ctrl}"], check=True)


def record_sentence(stream):
    silent_chunks = 0
    max_silent = int(SILENCE_SECONDS * SAMPLE_RATE / CHUNK)
    max_chunks = int(MAX_SENTENCE_SECONDS * SAMPLE_RATE / CHUNK)
    frames = []

    print("  Listening for sentence...")
    for _ in range(max_chunks):
        chunk = np.frombuffer(
            stream.read(CHUNK, exception_on_overflow=False), dtype=np.int16
        )
        frames.append(chunk)
        if np.sqrt(np.mean(chunk.astype(np.float32) ** 2)) < SILENCE_ENERGY:
            silent_chunks += 1
            if silent_chunks >= max_silent:
                break
        else:
            silent_chunks = 0

    return np.concatenate(frames).astype(np.float32) / 32768.0


def transcribe(whisper, audio):
    segments, _ = whisper.transcribe(audio, language="en")
    return " ".join(s.text.strip() for s in segments).strip()


def take_photo():
    cap = cv2.VideoCapture(CAMERA_INDEX)
    if not cap.isOpened():
        print("  Camera open failed.")
        return None
    try:
        for _ in range(WARMUP_FRAMES):
            cap.read()
        ret, frame = cap.read()
        if not ret:
            print("  Camera read failed.")
            return None
        frame = apply_clahe(frame)
        os.makedirs(PHOTOS_DIR, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = os.path.join(PHOTOS_DIR, f"{ts}.jpg")
        cv2.imwrite(path, frame)
        print(f"  Saved: {path}")
        return path
    finally:
        cap.release()


def send_photo(path, caption):
    with open(path, "rb") as f:
        data = f.read()
    msg = EmailMessage()
    msg["Subject"] = "Wesley captured a photo"
    msg["From"] = EMAIL_FROM
    msg["To"] = EMAIL_TO
    msg.set_content(caption or "(no speech detected)")
    msg.add_attachment(data, maintype="image", subtype="jpeg", filename=os.path.basename(path))
    with smtplib.SMTP("smtp.gmail.com", 587) as s:
        s.starttls()
        s.login(EMAIL_FROM, EMAIL_APP_PASSWORD)
        s.send_message(msg)
    print(f"  Emailed: {path}")


def main():
    print("Loading models...")
    # NOTE: "hey_jarvis" is the only wake-word model bundled with openwakeword.
    # A custom "hey_wesley" model would need to be trained (openwakeword
    # training pipeline) and loaded by file path. Until then we use hey_jarvis.
    oww = Model(wakeword_models=["hey_jarvis"], inference_framework="onnx")
    whisper = WhisperModel("tiny", device="cpu", compute_type="int8")

    set_camera_controls()

    pa = pyaudio.PyAudio()
    stream = pa.open(
        rate=SAMPLE_RATE,
        channels=1,
        format=pyaudio.paInt16,
        input=True,
        input_device_index=MIC_DEVICE_INDEX,
        frames_per_buffer=CHUNK,
    )

    print('Listening for "Hey Jarvis"... (Ctrl+C to quit)')
    cooldown = 0
    try:
        while True:
            audio = np.frombuffer(
                stream.read(CHUNK, exception_on_overflow=False), dtype=np.int16
            )
            prediction = oww.predict(audio)
            score = prediction.get("hey_jarvis", 0)

            if cooldown > 0:
                cooldown -= 1
            elif score >= THRESHOLD:
                print(f"Wake word detected! (score={score:.2f})")
                # Take photo in background so audio capture isn't blocked
                photo_result = [None]
                def _snap():
                    photo_result[0] = take_photo()
                snap_thread = threading.Thread(target=_snap, daemon=True)
                snap_thread.start()

                sentence_audio = record_sentence(stream)
                caption = transcribe(whisper, sentence_audio)
                print(f"  Transcribed: {caption!r}")

                snap_thread.join()
                if photo_result[0]:
                    send_photo(photo_result[0], caption)
                cooldown = 20
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        stream.stop_stream()
        stream.close()
        pa.terminate()


if __name__ == "__main__":
    main()
