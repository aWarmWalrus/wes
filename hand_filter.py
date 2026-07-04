import cv2
import pygame
import mediapipe as mp
import numpy as np
import sys
from collections import deque

CAMERA_INDEX = 0
WARMUP_FRAMES = 60
SMOOTH_N = 5

WAVE_SPEED = 8        # pixels expanded per frame
WAVE_THICKNESS = 4    # ring stroke width
WAVE_LIFETIME = 60    # frames before fully faded

FINGER_TIPS  = [8, 12, 16, 20]
FINGER_BASES = [6, 10, 14, 18]

def dist(a, b):
    return ((a.x - b.x) ** 2 + (a.y - b.y) ** 2) ** 0.5

def count_fingers(landmarks):
    count = 0
    hand_scale = dist(landmarks[0], landmarks[9])
    if dist(landmarks[4], landmarks[5]) > hand_scale * 0.4:
        count += 1
    for tip, base in zip(FINGER_TIPS, FINGER_BASES):
        if landmarks[tip].y < landmarks[base].y:
            count += 1
    return count

def hand_props(landmarks, w, h):
    palm = [0, 1, 5, 9, 13, 17]
    cx = sum(landmarks[i].x for i in palm) / len(palm)
    cy = sum(landmarks[i].y for i in palm) / len(palm)
    size = dist(landmarks[0], landmarks[12])
    radius = int(size * max(w, h) * 0.6)
    return int(cx * w), int(cy * h), radius

def main():
    pygame.init()
    screen = pygame.display.set_mode((0, 0), pygame.FULLSCREEN)
    W, H = screen.get_size()
    pygame.display.set_caption("WES - Hand Filter")
    clock = pygame.time.Clock()

    # Surface for alpha-blended wave rings
    wave_surf = pygame.Surface((W, H), pygame.SRCALPHA)

    cap = cv2.VideoCapture(CAMERA_INDEX)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)

    mp_hands_mod = mp.solutions.hands
    hands = mp_hands_mod.Hands(
        max_num_hands=2,
        min_detection_confidence=0.75,
        min_tracking_confidence=0.6,
    )

    smooth = {
        "Left":  {"buf": deque(maxlen=SMOOTH_N), "val": None, "was_open": False},
        "Right": {"buf": deque(maxlen=SMOOTH_N), "val": None, "was_open": False},
    }

    # Active waves: list of [cx, cy, radius, age]
    waves = []

    frame_count = 0

    while True:
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                cap.release()
                pygame.quit()
                sys.exit()
            if event.type == pygame.KEYDOWN and event.key == pygame.K_ESCAPE:
                cap.release()
                pygame.quit()
                sys.exit()

        ret, frame = cap.read()
        if not ret:
            continue

        frame_count += 1
        frame = cv2.flip(frame, 1)
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)

        circles = []
        seen = set()

        if frame_count > WARMUP_FRAMES:
            results = hands.process(rgb)
            if results.multi_hand_landmarks and results.multi_handedness:
                for hand_lm, handedness in zip(results.multi_hand_landmarks, results.multi_handedness):
                    lm = hand_lm.landmark
                    label = handedness.classification[0].label
                    seen.add(label)

                    fingers = count_fingers(lm)
                    filled = fingers >= 4
                    cx, cy, radius = hand_props(lm, W, H)

                    s = smooth[label]
                    s["buf"].append((cx, cy, radius, filled))
                    if len(s["buf"]) == SMOOTH_N:
                        avg_cx = int(sum(v[0] for v in s["buf"]) / SMOOTH_N)
                        avg_cy = int(sum(v[1] for v in s["buf"]) / SMOOTH_N)
                        avg_r  = int(sum(v[2] for v in s["buf"]) / SMOOTH_N)
                        avg_filled = sum(1 for v in s["buf"] if v[3]) >= 3
                        s["val"] = (avg_cx, avg_cy, avg_r, avg_filled)

                        # Spawn wave on closed → open transition
                        if avg_filled and not s["was_open"]:
                            waves.append([avg_cx, avg_cy, avg_r, 0])
                        s["was_open"] = avg_filled

                    if s["val"]:
                        circles.append(s["val"])

        for label in ("Left", "Right"):
            if label not in seen:
                smooth[label]["buf"].clear()
                smooth[label]["val"] = None
                smooth[label]["was_open"] = False

        # Advance waves
        for w in waves:
            w[2] += WAVE_SPEED  # expand radius
            w[3] += 1           # age
        waves = [w for w in waves if w[3] < WAVE_LIFETIME]

        # --- Draw ---
        screen.fill((0, 0, 0))

        # Waves (fading rings)
        wave_surf.fill((0, 0, 0, 0))
        for cx, cy, radius, age in waves:
            alpha = int(255 * (1 - age / WAVE_LIFETIME))
            pygame.draw.circle(wave_surf, (255, 255, 255, alpha), (cx, cy), radius, WAVE_THICKNESS)
        screen.blit(wave_surf, (0, 0))

        # Hand circles
        for cx, cy, radius, filled in circles:
            if filled:
                pygame.draw.circle(screen, (255, 255, 255), (cx, cy), radius)
            else:
                pygame.draw.circle(screen, (255, 255, 255), (cx, cy), radius, 4)

        pygame.display.flip()
        clock.tick(30)

if __name__ == "__main__":
    main()
