---
id: 010
title: Use spare Hailo headroom — pose/gesture, ambient detection, on-device wake word
status: done
priority: low
created: 2026-07-04
closed: 2026-09-02
tags: [hailo, vision]
related: [docs/vision.md, docs/hardware.md]
---

## Goal
The Hailo-8 does object detection (`look`) + face recognition (every wake word)
but has spare headroom (26 TOPS). Candidate uses:

- Pose/gesture as a tool — `yolov8s_pose` HEF is already present.
- Running detection on every turn for ambient context.
- Moving wake-word detection to the Hailo (NOT supported by openwakeword today
  — would need a custom/alternative model).

## Acceptance
- [ ] pick one candidate and ship it as a tool or ambient signal

## Notes
Exploratory; no pressing need. Pose is the lowest-effort (HEF present).

## CLOSED 2026-09-02 — obsolete, not shipped

The Raspberry Pi tier was retired: the owner repurposed the hardware, and with
it went the microphone, the speaker, the camera and the Hailo-8 accelerator that
every part of this ticket depended on. Nothing here was built. It is filed under
`done/` because that is where closed tickets live, not because it shipped.

The code it would have modified is in `archive/pi/`, and the design write-ups it
referenced moved to `docs/archive/`. If a Pi ever rejoins the system, this is
still a reasonable statement of the problem — read it with
`archive/pi/README.md` beside it.
