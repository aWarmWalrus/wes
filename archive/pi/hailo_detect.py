#!/usr/bin/env python3
"""On-device object detection for WES — run with SYSTEM python3 (has hailo_platform + cv2).

Captures one frame from the C920, runs YOLOv8s on the Hailo-8L (NMS baked into the
HEF), and prints JSON detections to stdout:

    {"objects": [{"label": "person", "conf": 0.94, "position": "center", "size": "large"}, ...]}

Debug goes to stderr. Intended to be shelled out to by the Pi state endpoint.
"""

import json
import sys

import cv2
import numpy as np
from hailo_platform import (
    HEF, VDevice, ConfigureParams, InferVStreams,
    InputVStreamParams, OutputVStreamParams, HailoStreamInterface, FormatType,
)

HEF_PATH = "/usr/share/hailo-models/yolov8s_h8.hef"  # device is Hailo-8 (not 8L)
CAMERA_INDEX = 0
CONF_THRESHOLD = 0.4
WARMUP_FRAMES = 5  # let auto-exposure settle

COCO = [
    "person", "bicycle", "car", "motorcycle", "airplane", "bus", "train", "truck",
    "boat", "traffic light", "fire hydrant", "stop sign", "parking meter", "bench",
    "bird", "cat", "dog", "horse", "sheep", "cow", "elephant", "bear", "zebra",
    "giraffe", "backpack", "umbrella", "handbag", "tie", "suitcase", "frisbee",
    "skis", "snowboard", "sports ball", "kite", "baseball bat", "baseball glove",
    "skateboard", "surfboard", "tennis racket", "bottle", "wine glass", "cup",
    "fork", "knife", "spoon", "bowl", "banana", "apple", "sandwich", "orange",
    "broccoli", "carrot", "hot dog", "pizza", "donut", "cake", "chair", "couch",
    "potted plant", "bed", "dining table", "toilet", "tv", "laptop", "mouse",
    "remote", "keyboard", "cell phone", "microwave", "oven", "toaster", "sink",
    "refrigerator", "book", "clock", "vase", "scissors", "teddy bear",
    "hair drier", "toothbrush",
]


def eprint(*a):
    print(*a, file=sys.stderr, flush=True)


def capture_frame():
    cap = cv2.VideoCapture(CAMERA_INDEX)
    if not cap.isOpened():
        raise RuntimeError("camera open failed")
    try:
        for _ in range(WARMUP_FRAMES):
            cap.read()
        ok, frame = cap.read()
        if not ok:
            raise RuntimeError("camera read failed")
        return frame  # BGR
    finally:
        cap.release()


def run_hailo(frame_rgb):
    hef = HEF(HEF_PATH)
    with VDevice() as target:
        cfg = ConfigureParams.create_from_hef(
            hef, interface=HailoStreamInterface.PCIe
        )
        network_group = target.configure(hef, cfg)[0]
        ng_params = network_group.create_params()
        in_params = InputVStreamParams.make(network_group, format_type=FormatType.UINT8)
        out_params = OutputVStreamParams.make(network_group, format_type=FormatType.FLOAT32)
        in_info = hef.get_input_vstream_infos()[0]
        out_info = hef.get_output_vstream_infos()[0]
        data = {in_info.name: np.expand_dims(frame_rgb, axis=0).astype(np.uint8)}
        with InferVStreams(network_group, in_params, out_params) as pipeline:
            with network_group.activate(ng_params):
                results = pipeline.infer(data)
        return results[out_info.name]


def _pos(cx):
    return "left" if cx < 0.33 else ("right" if cx > 0.66 else "center")


def _size(area):
    return "small" if area < 0.05 else ("large" if area > 0.25 else "medium")


def parse(nms_output):
    """Parse HAILO NMS BY_CLASS output into a list of detections.

    Expected: nms_output[0] is a list of length 80 (one per class); each entry is
    an (n, 5) array of [y_min, x_min, y_max, x_max, score], coords normalized 0-1.
    """
    objs = []
    per_class = nms_output[0] if len(nms_output) else []
    eprint(f"[dbg] output type={type(nms_output).__name__} "
           f"len={len(nms_output)} per_class_len={len(per_class)}")
    for cls_id, dets in enumerate(per_class):
        if dets is None or len(dets) == 0:
            continue
        for d in dets:
            score = float(d[4])
            if score < CONF_THRESHOLD:
                continue
            ymin, xmin, ymax, xmax = float(d[0]), float(d[1]), float(d[2]), float(d[3])
            cx = (xmin + xmax) / 2
            area = max(0.0, (xmax - xmin)) * max(0.0, (ymax - ymin))
            objs.append({
                "label": COCO[cls_id] if cls_id < len(COCO) else str(cls_id),
                "conf": round(score, 2),
                "position": _pos(cx),
                "size": _size(area),
            })
    objs.sort(key=lambda o: o["conf"], reverse=True)
    return objs


def main():
    frame = capture_frame()
    eprint(f"[dbg] frame shape={frame.shape}")
    rgb = cv2.cvtColor(cv2.resize(frame, (640, 640)), cv2.COLOR_BGR2RGB)
    out = run_hailo(rgb)
    objs = parse(out)
    print(json.dumps({"objects": objs}))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001
        print(json.dumps({"error": str(e)}))
        eprint(f"[err] {e!r}")
        sys.exit(1)
