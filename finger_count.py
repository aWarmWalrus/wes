import cv2
import pygame
import mediapipe as mp
import numpy as np
import sys
from collections import deque

# --- Config ---
CAMERA_INDEX = 0
DISPLAY_W, DISPLAY_H = 1280, 720
WARMUP_FRAMES = 60
SMOOTH_N = 5  # frames of agreement before updating displayed number

# One color per number 0-5
NUMBER_COLORS = [
    (180, 180, 180),  # 0 - grey
    (255,  80,  80),  # 1 - red
    (255, 165,   0),  # 2 - orange
    (255, 230,   0),  # 3 - yellow
    ( 60, 200,  80),  # 4 - green
    ( 80, 130, 255),  # 5 - blue
]

# --- Finger counting ---
FINGER_TIPS  = [8, 12, 16, 20]
FINGER_BASES = [6, 10, 14, 18]

def dist(a, b):
    return ((a.x - b.x) ** 2 + (a.y - b.y) ** 2) ** 0.5

def count_fingers(landmarks, handedness):
    count = 0
    # Thumb: tip far from index MCP relative to hand size
    hand_scale = dist(landmarks[0], landmarks[9])  # wrist to middle MCP
    if dist(landmarks[4], landmarks[5]) > hand_scale * 0.4:
        count += 1
    # Other four fingers: tip y above PIP joint
    for tip, base in zip(FINGER_TIPS, FINGER_BASES):
        if landmarks[tip].y < landmarks[base].y:
            count += 1
    return count

def hand_center(landmarks, w, h):
    """Screen-space center using palm landmarks."""
    palm = [0, 1, 5, 9, 13, 17]
    x = sum(landmarks[i].x for i in palm) / len(palm)
    y = sum(landmarks[i].y for i in palm) / len(palm)
    return int(x * w), int(y * h)

def draw_number(screen, font, count, cx, cy):
    color = NUMBER_COLORS[count]
    shadow = font.render(str(count), True, (0, 0, 0))
    screen.blit(shadow, shadow.get_rect(center=(cx + 5, cy + 5)))
    text = font.render(str(count), True, color)
    screen.blit(text, text.get_rect(center=(cx, cy)))

# --- Main ---
def main():
    pygame.init()
    screen = pygame.display.set_mode((0, 0), pygame.FULLSCREEN)
    DISPLAY_W, DISPLAY_H = screen.get_size()
    pygame.display.set_caption("WES - Finger Counter")
    clock = pygame.time.Clock()

    font_size = int(DISPLAY_H * 0.35)
    try:
        font = pygame.font.SysFont("dejavusans", font_size, bold=True)
    except Exception:
        font = pygame.font.Font(None, font_size)

    label_font_size = int(DISPLAY_H * 0.07)
    try:
        label_font = pygame.font.SysFont("dejavusans", label_font_size)
    except Exception:
        label_font = pygame.font.Font(None, label_font_size)

    cap = cv2.VideoCapture(CAMERA_INDEX)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)

    mp_hands_mod = mp.solutions.hands
    hands = mp_hands_mod.Hands(
        max_num_hands=2,
        min_detection_confidence=0.75,
        min_tracking_confidence=0.6,
    )

    # Per-hand smoothing buffers keyed by "Left" / "Right"
    smooth_buffers = {"Left": deque(maxlen=SMOOTH_N), "Right": deque(maxlen=SMOOTH_N)}
    smoothed = {"Left": None, "Right": None}

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

        detected_hands = []  # list of (count, cx, cy, label)
        seen_labels = set()

        if frame_count > WARMUP_FRAMES:
            results = hands.process(rgb)
            if results.multi_hand_landmarks and results.multi_handedness:
                for hand_lm, handedness in zip(results.multi_hand_landmarks, results.multi_handedness):
                    lm = hand_lm.landmark
                    label = handedness.classification[0].label
                    count = count_fingers(lm, label)
                    cx, cy = hand_center(lm, DISPLAY_W, DISPLAY_H)
                    seen_labels.add(label)

                    buf = smooth_buffers[label]
                    buf.append(count)
                    if len(buf) == SMOOTH_N and len(set(buf)) == 1:
                        smoothed[label] = buf[0]

                    if smoothed[label] is not None:
                        detected_hands.append((smoothed[label], cx, cy))

        # Clear smoothing for hands no longer visible
        for label in ("Left", "Right"):
            if label not in seen_labels:
                smooth_buffers[label].clear()
                smoothed[label] = None

        # --- Draw ---
        # Fix: use np.transpose to correctly orient the frame (no rotation)
        cam_surface = pygame.surfarray.make_surface(np.transpose(rgb, (1, 0, 2)))
        cam_scaled = pygame.transform.scale(cam_surface, (DISPLAY_W, DISPLAY_H))

        dim = pygame.Surface((DISPLAY_W, DISPLAY_H), pygame.SRCALPHA)
        dim.fill((0, 0, 0, 140))
        cam_scaled.blit(dim, (0, 0))
        screen.blit(cam_scaled, (0, 0))

        if detected_hands:
            for count, cx, cy in detected_hands:
                draw_number(screen, font, count, cx, cy)
        elif frame_count > WARMUP_FRAMES:
            msg = label_font.render("Show your hand!", True, (255, 255, 255))
            screen.blit(msg, msg.get_rect(center=(DISPLAY_W // 2, DISPLAY_H // 2)))
        else:
            msg = label_font.render("Getting ready...", True, (200, 200, 200))
            screen.blit(msg, msg.get_rect(center=(DISPLAY_W // 2, DISPLAY_H // 2)))

        pygame.display.flip()
        clock.tick(30)

if __name__ == "__main__":
    main()
