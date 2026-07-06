---
id: 010
title: Use spare Hailo headroom — pose/gesture, ambient detection, on-device wake word
status: open
priority: low
created: 2026-07-04
closed:
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
