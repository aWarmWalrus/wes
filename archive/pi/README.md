# Retired: the Raspberry Pi tier (2026-09-02)

WES used to be three tiers. Tier 1 was a **Raspberry Pi 5 + Hailo-8** at
`10.0.0.79` that owned everything physical: the "hey jarvis" wake word, mic
capture with Silero-VAD endpointing, Bluetooth playback to a JBL speaker,
on-device vision (SCRFD + ArcFace faces, YOLOv8s objects) on the Hailo
accelerator, and — separately from all of that — the Prometheus and Grafana
instances that scraped the PC.

**The owner repurposed the Pi for other projects.** Nothing in the live system
talks to it any more. WES is now a single-machine, text-first assistant: the
Discord bot and the fantasy GM talk to `pc/wes_server.py` on the PC, and
monitoring moved onto the PC in Docker (`observability/docker-compose.yml`).

This directory is kept as a **reference, not as runnable code.** None of it is
imported, tested, or deployed, and the CI suite does not collect it.

## What is here

| File | What it did |
|---|---|
| `wes_client.py` | The tier-1 client: openwakeword, Silero-VAD endpointing, utterance capture, single-stream `play_turn` playback, BT monitor + status LED |
| `wes-client.service` | systemd **user** unit that kept the client alive across reboots |
| `hailo_faces.py` | SCRFD detect + ArcFace recognize + clothing-colour tag, on the Hailo (needed the Pi's **system** python3, not the venv) |
| `hailo_detect.py` | YOLOv8s object detection on the Hailo |
| `capture_frame.py` | Grab a single camera frame |
| `pi_state.py` | Read-only state/vision endpoint on `:8090` — `/state`, `/look`, `/scene`, `/frame`, `/logs`. The PC server's `get_system_status`, `look`, `describe_scene` and `read_pi_log` tools all called this |
| `test_faces.py` | Face-recognition test; ran on the Pi under system python3, never collected by pytest |
| `test_arcface.py` | ArcFace embedding sanity check |
| `wes-mdns-hosts.*` | systemd service/timer that kept the Pi's `/etc/hosts` pointed at the PC's flapping DHCP lease |
| `speak.py` | Standalone piper→catt utility that cast speech to a Google Home device. Its paths (`~/cast-venv/bin/piper`, `~/wes/voices/`) were the **Pi's**, not the PC's |

## What went with it, on the PC side

Removed from `pc/wes_server.py` in the same change, because the Pi was the only
producer or consumer:

- **Endpoints** `/respond`, `/respond_stream` (WAV in, PCM out), `/speculate`,
  `/prefetch_scene`
- **Tools** `get_system_status`, `look`, `describe_scene`, `read_pi_log`
- **STT** (faster-whisper) and **TTS** (piper) — no microphone feeds the first,
  no speaker consumes the second
- The speculative-prefetch cache, which existed to hide STT latency
- Scene/face context injection into the system prompt

Everything is recoverable from git history; `git log -- pi/` reaches the full
record, and the design write-ups it implemented are in `docs/archive/`
(`pipeline.md`, `audio.md`, `vision.md`, `hardware.md`).

## If a Pi ever comes back

The client speaks a small, documented HTTP protocol, so reviving it is a matter
of restoring the four endpoints above rather than rewriting the client. Read
`docs/archive/pipeline.md` first — it describes the turn lifecycle these files
implement, including why playback is a single stream and where the latency went.
