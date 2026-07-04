#!/usr/bin/env python3
"""Fast unit tests for the pure face-pipeline logic — run with SYSTEM python3 on
the Pi (needs cv2/numpy; no Hailo or camera required):

    python3 ~/claude/wes/tests/test_faces.py

Covers the numpy/cv2 helpers that are easy to break silently: recognition
matching, position/clothing tagging, SCRFD box decode + NMS, gallery I/O.
"""
import os
import sys
import tempfile

import numpy as np

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "pi"))
import hailo_faces as hf  # noqa: E402

_pass = _fail = 0


def check(name, cond):
    global _pass, _fail
    if cond:
        _pass += 1
        print(f"  PASS {name}")
    else:
        _fail += 1
        print(f"  FAIL {name}")


# --- _position ---
check("position left", hf._position(50, 640) == "left")
check("position center", hf._position(320, 640) == "center")
check("position right", hf._position(600, 640) == "right")

# --- match (cosine vs gallery) ---
g = {"charlie": np.array([[1, 0, 0]], dtype=np.float32)}
name, sim = hf.match(np.array([1, 0, 0], dtype=np.float32), g)
check("match exact", name == "charlie" and abs(sim - 1.0) < 1e-5)
name2, _ = hf.match(np.array([0, 1, 0], dtype=np.float32), g)  # orthogonal
check("match orthogonal -> unknown", name2 == "unknown")
g2 = {"a": np.array([[1, 0, 0], [0, 1, 0]], dtype=np.float32)}
n3, s3 = hf.match(np.array([0, 1, 0], dtype=np.float32), g2)
check("match best-of-many embeddings", n3 == "a" and abs(s3 - 1.0) < 1e-5)

# --- SCRFD decode helpers ---
pts = np.array([[10, 10]], dtype=np.float32)
dist = np.array([[1, 2, 3, 4]], dtype=np.float32)
b = hf._distance2bbox(pts, dist)[0]
check("distance2bbox", list(b) == [9, 8, 13, 14])
dets = np.array([
    [0, 0, 10, 10, 0.9],
    [1, 1, 11, 11, 0.8],       # heavily overlaps the first -> suppressed
    [100, 100, 110, 110, 0.7],  # far away -> kept
], dtype=np.float32)
check("nms keeps 2 of 3", len(hf._nms(dets, 0.4)) == 2)


# --- _clothing_color (synthetic solid images, BGR) ---
def solid(bgr):
    img = np.zeros((100, 100, 3), dtype=np.uint8)
    img[:] = bgr
    return img


box = [30, 5, 70, 25]  # small face near the top so the torso region is in-frame
check("clothing red", hf._clothing_color(solid((0, 0, 255)), box) == "red")
check("clothing blue", hf._clothing_color(solid((255, 0, 0)), box) == "blue")
check("clothing black", hf._clothing_color(solid((0, 0, 0)), box) == "black")

# --- gallery round-trip ---
_old = hf.GALLERY_PATH
try:
    hf.GALLERY_PATH = os.path.join(tempfile.gettempdir(), "wes_test_gallery.json")
    if os.path.exists(hf.GALLERY_PATH):
        os.remove(hf.GALLERY_PATH)
    hf.save_gallery({"x": np.array([[1, 2, 3]], dtype=np.float32)})
    loaded = hf.load_gallery()
    check("gallery round-trip", "x" in loaded and loaded["x"].shape == (1, 3))
finally:
    hf.GALLERY_PATH = _old

print(f"\n{_pass} passed, {_fail} failed")
sys.exit(1 if _fail else 0)
