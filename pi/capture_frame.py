#!/usr/bin/env python3
"""Capture one JPEG frame from the C920 and write it to stdout (binary).

Run with SYSTEM python3 (has cv2). Used by the Pi state endpoint's /frame route
to hand a still image to the PC's Gemma vision tool.
"""

import sys

import cv2

CAMERA_INDEX = 0
WARMUP_FRAMES = 5
JPEG_QUALITY = 85


def main():
    cap = cv2.VideoCapture(CAMERA_INDEX)
    if not cap.isOpened():
        sys.exit(1)
    try:
        for _ in range(WARMUP_FRAMES):
            cap.read()
        ok, frame = cap.read()
    finally:
        cap.release()
    if not ok:
        sys.exit(1)
    ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
    if not ok:
        sys.exit(1)
    sys.stdout.buffer.write(buf.tobytes())


if __name__ == "__main__":
    main()
