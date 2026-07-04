#!/usr/bin/env python3
"""Face detection + recognition on the Hailo-8 (run with system python3).

Stage 1: SCRFD-10g -> face boxes + 5 landmarks (manual anchor decode + NMS).
Stage 2: align each face -> ArcFace -> 512-d embedding.

This file first proves detection works (main prints detected faces). Alignment,
embedding, enrollment, and matching build on top.
"""

import json
import os
import sys
import time

import cv2
import numpy as np
from hailo_platform import (
    HEF, VDevice, ConfigureParams, InferVStreams,
    InputVStreamParams, OutputVStreamParams, HailoStreamInterface, FormatType,
)

SCRFD_HEF = "/usr/local/hailo/resources/models/hailo8/scrfd_10g.hef"
ARCFACE_HEF = "/usr/local/hailo/resources/models/hailo8/arcface_mobilefacenet.hef"
CAMERA_INDEX = 0
WARMUP_FRAMES = 5
SCORE_THRESH = 0.5
NMS_THRESH = 0.4
INPUT_SIZE = 640
STRIDES = [8, 16, 32]
NUM_ANCHORS = 2


def eprint(*a):
    print(*a, file=sys.stderr, flush=True)


def _infer(hef_path, image_nhwc):
    """Run a HEF on a single NHWC uint8 image; return {output_name: np.array}."""
    hef = HEF(hef_path)
    with VDevice() as target:
        cfg = ConfigureParams.create_from_hef(hef, interface=HailoStreamInterface.PCIe)
        ng = target.configure(hef, cfg)[0]
        ngp = ng.create_params()
        inp = InputVStreamParams.make(ng, format_type=FormatType.UINT8)
        outp = OutputVStreamParams.make(ng, format_type=FormatType.FLOAT32)
        in_info = hef.get_input_vstream_infos()[0]
        with InferVStreams(ng, inp, outp) as pipe:
            with ng.activate(ngp):
                res = pipe.infer({in_info.name: np.expand_dims(image_nhwc, 0).astype(np.uint8)})
    return res


def _distance2bbox(points, distance):
    x1 = points[:, 0] - distance[:, 0]
    y1 = points[:, 1] - distance[:, 1]
    x2 = points[:, 0] + distance[:, 2]
    y2 = points[:, 1] + distance[:, 3]
    return np.stack([x1, y1, x2, y2], axis=-1)


def _distance2kps(points, distance):
    kps = []
    for i in range(0, distance.shape[1], 2):
        px = points[:, 0] + distance[:, i]
        py = points[:, 1] + distance[:, i + 1]
        kps.append(px)
        kps.append(py)
    return np.stack(kps, axis=-1)


def _nms(dets, thresh):
    x1, y1, x2, y2, scores = dets[:, 0], dets[:, 1], dets[:, 2], dets[:, 3], dets[:, 4]
    areas = (x2 - x1 + 1) * (y2 - y1 + 1)
    order = scores.argsort()[::-1]
    keep = []
    while order.size > 0:
        i = order[0]
        keep.append(i)
        xx1 = np.maximum(x1[i], x1[order[1:]])
        yy1 = np.maximum(y1[i], y1[order[1:]])
        xx2 = np.minimum(x2[i], x2[order[1:]])
        yy2 = np.minimum(y2[i], y2[order[1:]])
        w = np.maximum(0.0, xx2 - xx1 + 1)
        h = np.maximum(0.0, yy2 - yy1 + 1)
        inter = w * h
        ovr = inter / (areas[i] + areas[order[1:]] - inter)
        order = order[1:][ovr <= thresh]
    return keep


def _find_output(res, channels, hw):
    """Locate the SCRFD output tensor with the given channel count + spatial size."""
    for name, arr in res.items():
        a = np.array(arr)
        a = a.reshape(a.shape[-3], a.shape[-2], a.shape[-1]) if a.ndim == 4 else a
        if a.shape[0] == hw and a.shape[1] == hw and a.shape[2] == channels:
            return a
    return None


def detect_faces(frame_bgr):
    """Return list of {box:[x1,y1,x2,y2], score, kps:[[x,y]*5]} in frame pixels."""
    h0, w0 = frame_bgr.shape[:2]
    img = cv2.resize(frame_bgr, (INPUT_SIZE, INPUT_SIZE))
    rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
    res = _infer(SCRFD_HEF, rgb)
    boxes, scores, kpss = [], [], []
    for stride in STRIDES:
        hw = INPUT_SIZE // stride
        score = _find_output(res, NUM_ANCHORS * 1, hw)
        bbox = _find_output(res, NUM_ANCHORS * 4, hw)
        kps = _find_output(res, NUM_ANCHORS * 10, hw)
        if score is None or bbox is None or kps is None:
            eprint(f"[dbg] stride {stride}: missing outputs")
            continue
        score = score.reshape(-1)
        bbox = bbox.reshape(-1, 4) * stride
        kps = kps.reshape(-1, 10) * stride
        # anchor centers: grid * stride, each repeated NUM_ANCHORS times
        ys, xs = np.mgrid[0:hw, 0:hw]
        centers = np.stack([xs.ravel(), ys.ravel()], axis=-1).astype(np.float32) * stride
        centers = np.repeat(centers, NUM_ANCHORS, axis=0)
        keep = score >= SCORE_THRESH
        if not keep.any():
            continue
        b = _distance2bbox(centers[keep], bbox[keep])
        k = _distance2kps(centers[keep], kps[keep])
        boxes.append(b)
        scores.append(score[keep])
        kpss.append(k)

    if not boxes:
        return []
    boxes = np.vstack(boxes)
    scores = np.concatenate(scores)
    kpss = np.vstack(kpss)
    # scale back to original frame
    sx, sy = w0 / INPUT_SIZE, h0 / INPUT_SIZE
    boxes[:, [0, 2]] *= sx
    boxes[:, [1, 3]] *= sy
    kpss[:, 0::2] *= sx
    kpss[:, 1::2] *= sy
    dets = np.hstack([boxes, scores[:, None]])
    keep = _nms(dets, NMS_THRESH)
    out = []
    for i in keep:
        out.append({
            "box": [float(v) for v in boxes[i]],
            "score": float(scores[i]),
            "kps": [[float(kpss[i][j]), float(kpss[i][j + 1])] for j in range(0, 10, 2)],
        })
    return out


# Canonical ArcFace 5-point template for a 112x112 aligned face (insightface).
_REF_LANDMARKS = np.array([
    [38.2946, 51.6963], [73.5318, 51.5014], [56.0252, 71.7366],
    [41.5493, 92.3655], [70.7299, 92.2041],
], dtype=np.float32)


def align_face(frame_bgr, kps):
    """Similarity-warp a face to a 112x112 aligned crop using its 5 landmarks."""
    src = np.array(kps, dtype=np.float32)
    M, _ = cv2.estimateAffinePartial2D(src, _REF_LANDMARKS, method=cv2.LMEDS)
    if M is None:
        return None
    return cv2.warpAffine(frame_bgr, M, (112, 112), borderValue=0)


def embed_face(aligned_bgr):
    """ArcFace embedding (L2-normalized 512-d) for an aligned 112x112 face."""
    rgb = cv2.cvtColor(aligned_bgr, cv2.COLOR_BGR2RGB)
    res = _infer(ARCFACE_HEF, rgb)
    emb = np.array(next(iter(res.values()))).flatten().astype(np.float32)
    return emb / (np.linalg.norm(emb) + 1e-9)


def capture_frame():
    cap = cv2.VideoCapture(CAMERA_INDEX)
    try:
        for _ in range(WARMUP_FRAMES):
            cap.read()
        ok, frame = cap.read()
    finally:
        cap.release()
    if not ok:
        raise RuntimeError("camera read failed")
    return frame


# --- Enrollment + matching (the face "gallery") ----------------------------

GALLERY_PATH = os.path.expanduser("~/wes/known_faces.json")
MATCH_THRESHOLD = 0.45      # cosine similarity; tune after testing
MAX_SAMPLES_PER_PERSON = 40  # keep the most recent N embeddings per person


def load_gallery():
    """{name: ndarray (K, 512)} — K stored embeddings per person."""
    if os.path.exists(GALLERY_PATH):
        with open(GALLERY_PATH) as f:
            d = json.load(f)
        return {n: np.array(v, dtype=np.float32) for n, v in d.items()}
    return {}


def save_gallery(g):
    os.makedirs(os.path.dirname(GALLERY_PATH), exist_ok=True)
    with open(GALLERY_PATH, "w") as f:
        json.dump({n: e.tolist() for n, e in g.items()}, f)


def _largest(faces):
    if not faces:
        return None
    return max(faces, key=lambda f: (f["box"][2] - f["box"][0]) * (f["box"][3] - f["box"][1]))


def _embed_largest(frame):
    f = _largest(detect_faces(frame))
    if f is None:
        return None
    aligned = align_face(frame, f["kps"])
    return embed_face(aligned) if aligned is not None else None


def enroll(name, num=12):
    """Capture several frames of one person, average their embeddings, store it."""
    cap = cv2.VideoCapture(CAMERA_INDEX)
    embs = []
    try:
        for _ in range(WARMUP_FRAMES):
            cap.read()
        for _ in range(num):
            ok, frame = cap.read()
            if not ok:
                continue
            e = _embed_largest(frame)
            if e is not None:
                embs.append(e)
                eprint(f"  captured sample {len(embs)}")
            time.sleep(0.25)
    finally:
        cap.release()
    if not embs:
        print("No face captured — face the camera and try again.")
        return
    new = np.array(embs, dtype=np.float32)  # (n, 512), each already L2-normalized
    g = load_gallery()
    combined = np.vstack([g[name], new]) if name in g else new
    if len(combined) > MAX_SAMPLES_PER_PERSON:
        combined = combined[-MAX_SAMPLES_PER_PERSON:]  # keep most recent
    g[name] = combined
    save_gallery(g)
    print(f"Enrolled {name!r}: +{len(embs)} new samples ({len(combined)} total). "
          f"Known people: {list(g)}")


def forget(name):
    """Remove one person's samples from the gallery."""
    g = load_gallery()
    if name in g:
        del g[name]
        save_gallery(g)
        print(f"Forgot {name!r}. Known people: {list(g)}")
    else:
        print(f"{name!r} not enrolled. Known people: {list(g)}")


def match(emb, gallery):
    """Best cosine similarity vs. any stored embedding of any person."""
    best_name, best = "unknown", 0.0
    for name, refs in gallery.items():
        sim = float((refs @ emb).max())  # refs (K,512) @ emb (512,) -> (K,), take max
        if sim > best:
            best, best_name = sim, name
    return (best_name if best >= MATCH_THRESHOLD else "unknown", best)


def _position(cx, width):
    r = cx / width
    return "left" if r < 0.33 else ("right" if r > 0.66 else "center")


# OpenCV hue (0-180) -> coarse color name, for a quick clothing discriminator.
_HUE_NAMES = [(10, "red"), (20, "orange"), (35, "yellow"), (85, "green"),
              (100, "cyan"), (130, "blue"), (160, "purple"), (170, "pink"), (180, "red")]


def _clothing_color(frame, box):
    """Dominant color of the torso (region just below the face) as a color word.

    A cheap, model-free tag used to tell adjacent people apart, not a full
    description (Gemma does that)."""
    h0, w0 = frame.shape[:2]
    x1, y1, x2, y2 = box
    fw, fh, cx = x2 - x1, y2 - y1, (x1 + x2) / 2
    tx1, tx2 = int(max(0, cx - fw * 0.75)), int(min(w0, cx + fw * 0.75))
    ty1, ty2 = int(min(h0, y2)), int(min(h0, y2 + fh * 1.5))
    if ty2 - ty1 < 5 or tx2 - tx1 < 5:
        return "unknown"
    hsv = cv2.cvtColor(frame[ty1:ty2, tx1:tx2], cv2.COLOR_BGR2HSV).reshape(-1, 3)
    hue, sat, val = (float(x) for x in np.median(hsv, axis=0))
    if val < 50:
        return "black"
    if sat < 40:
        return "white" if val > 180 else "gray"
    for hi, name in _HUE_NAMES:
        if hue <= hi:
            return name
    return "red"


def recognize(frame):
    """Detect + match faces -> [{name, similarity, position, clothing}]."""
    gallery = load_gallery()
    width = frame.shape[1]
    out = []
    for f in detect_faces(frame):
        aligned = align_face(frame, f["kps"])
        name, sim = ("unknown", 0.0)
        if aligned is not None:
            name, sim = match(embed_face(aligned), gallery)
        cx = (f["box"][0] + f["box"][2]) / 2
        out.append({"name": name, "similarity": round(sim, 3),
                    "position": _position(cx, width),
                    "clothing": _clothing_color(frame, f["box"])})
    return out


def scene():
    """Capture one frame, recognize faces, and return it as JSON with the JPEG.

    {"faces": [{name, similarity, position}], "jpeg_b64": "..."} — one capture used
    for both identity (Hailo) and the image handed to Gemma."""
    import base64

    frame = capture_frame()
    faces = recognize(frame)
    ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
    return {"faces": faces, "jpeg_b64": base64.b64encode(buf.tobytes()).decode() if ok else ""}


def main():
    frame = capture_frame()
    eprint("[dbg] frame:", frame.shape)
    faces = detect_faces(frame)
    print(f"detected {len(faces)} face(s)")
    for i, f in enumerate(faces):
        aligned = align_face(frame, f["kps"])
        emb = embed_face(aligned) if aligned is not None else None
        norm = float(np.linalg.norm(emb)) if emb is not None else None
        print(f"  face {i}: score={f['score']:.2f} box={[round(v) for v in f['box']]} "
              f"embedding={'ok, dim %d, norm %.3f' % (emb.shape[0], norm) if emb is not None else 'FAILED'}")


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "enroll":
        enroll(sys.argv[2])
    elif len(sys.argv) > 2 and sys.argv[1] == "forget":
        forget(sys.argv[2])
    elif len(sys.argv) > 1 and sys.argv[1] == "recognize":
        print(json.dumps({"faces": recognize(capture_frame())}))
    elif len(sys.argv) > 1 and sys.argv[1] == "scene":
        print(json.dumps(scene()))
    else:
        main()
