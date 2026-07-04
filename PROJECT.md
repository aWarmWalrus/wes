# WES — Walrus Embedded Assistant

A Raspberry Pi 5-based smart digital assistant with speech I/O and peripheral access.

## Hardware

- **Raspberry Pi 5** (16GB RAM)
- **Raspberry Pi AI Hat+** (Hailo-8L, ~13 TOPS) — on-device inference — **verified operational** (firmware 4.20.0, `/dev/hailo0`)
- **Logitech C920 PRO HD Webcam** — USB, 1080p capable, includes built-in microphone, see [[PERIPHERALS.md]] for specs
- **Speaker** — TBD model
- **General peripherals** — GPIO, USB access

## Concept

WES is a conversational AI assistant running locally on the Pi. It listens for voice input,
processes queries via speech-to-text, responds via text-to-speech, and can control connected
peripherals. An adaptive wake-up mechanism engages the system when ambient noise exceeds
a threshold, reducing always-on CPU usage.

## Capabilities

- **Speech-to-text (STT)** — convert user speech to text
- **Text-to-speech (TTS)** — speak responses back to user
- **Noise-triggered wake-up** — detect high ambient noise and activate listening
- **Peripheral access** — control GPIO/USB devices
- **On-device inference** — leverage AI Hat+ for local processing

## Project Status

- [x] AI Hat+ / Hailo pipeline setup (firmware 4.20.0, driver loaded)
- [ ] Microphone & speaker setup
- [ ] STT/TTS pipeline (local or cloud-optimized)
- [ ] Noise detection & adaptive wake-up
- [ ] Peripheral access layer
- [ ] First end-to-end voice interaction
