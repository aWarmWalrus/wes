# Hardware

## Raspberry Pi 5 (Tier 1)

- Raspberry Pi 5, 16GB RAM.
- **Raspberry Pi AI Hat+** (Hailo-8, ~26 TOPS — not the 8L; see `docs/vision.md` for
  which HEFs run where). Firmware 4.20.0, driver loaded (`/dev/hailo0`).
- GPIO/USB expansion present, not yet used for peripheral control.

## Camera — Logitech C920 PRO HD Webcam

- USB 2.0 (046d:08e5), driver `uvcvideo` (native Linux, no proprietary driver needed).
- Device files: `/dev/video0`, `/dev/video1`, `/dev/media3`.
- Formats: YUYV and MJPEG, 160x90 up to 2560x1472. Practical use: MJPEG at
  640x480/800x600 for real-time (30fps); 720p/1080p MJPEG for higher-res capture.
  Vision inference (`docs/vision.md`) runs on lower-res frames (480p-720p).
- Built-in mic is not used for STT (see `docs/setup.md` — Pi audio input is
  PulseAudio's default source, not the C920 directly).
- The C920's front LED doubles as the assistant's status light — see
  "Status LED" in `docs/pipeline.md`.

## Speaker

Bluetooth JBL Flip 5 is the primary output; Google Cast devices are the fallback.
Full details, pairing gotchas, and the A2DP stability fix: `docs/audio.md`.
