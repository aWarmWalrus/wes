# On-device vision

(Written when Claude was the LLM tier — read "Claude" below as "the router
LLM"; since 2026-07-16 that's local **gemma4:12b**, which is also the VLM — one
model serves chat, escalation, and vision. The mechanics are backend-agnostic.)

Three vision capabilities, all fed to the LLM as tools or as prefetched context:
**object detection** (Hailo YOLOv8s), **rich scene description** (local Gemma VLM), and
**face recognition** (Hailo SCRFD + ArcFace). The Hailo code runs under the Pi's
**system python3** (has `hailo_platform` + `cv2`, which the venv does not).

## `look` — fast object detection (Hailo YOLOv8s)

- `pi/hailo_detect.py`: captures a C920 frame, runs **YOLOv8s** on the Hailo
  (`/usr/share/hailo-models/yolov8s_h8.hef` — the Hat+ is a **Hailo-8**, not 8L), prints
  JSON. NMS is baked into the HEF, so postprocessing is trivial. ~1.6s end to end.
  Output: `{"objects":[{label,conf,position,size}]}` (position = box center-x
  left/center/right; size = box area).
- Pi `/look` route shells out to it; PC `look` tool calls `/look`; Claude narrates.
- Other HEFs on the Pi for future tools: pose (`yolov8s_pose`), seg (`yolov5n_seg`).

## `describe_scene` — rich description (local Gemma VLM)

- **Ollama** on the PC runs **`gemma4:12b`** (multimodal, 8.1GB VRAM, 100% on the
  5060 Ti), kept resident via `keep_alive: -1` + startup warmup. **Measured warm
  inference ≈ 15s per frame** (image prefill dominates) — acceptable only because the
  wake-word prefetch hides it; an ad-hoc cache-miss `describe_scene` is slow.
  `WES_VLM_MODEL` sets it (launcher points it at the same `gemma4:12b` that serves
  chat; the code default in `wes_server.py` is still the old `gemma3:4b`). Since the
  2026-07-16 collapse to one model, the router **is** the VLM — no separate vision
  model to co-resident. (History: the old `gemma4:e4b` router **couldn't** serve as
  the VLM — its Ollama build ignored `images` on `/api/generate`, "no image was
  provided", verified 2026-07-04 — which is part of why 12b was kept for vision.)
  `gemma3:4b` sees but has **no `tools`**, so it can never be the router.
- Flow: Pi `capture_frame.py` (system python3 + cv2) → JPEG → PC `describe_scene` →
  Gemma → Claude relays. Single frame, **not** a video stream.
- Claude chooses: `look` (fast object list) vs `describe_scene` (rich, Gemma).

## Face recognition (`pi/hailo_faces.py`)

Two-stage on-device pipeline: **SCRFD-10g** face detect + 5 landmarks (manual anchor
decode + NMS — no NMS baked into this HEF) → align to the ArcFace template → **ArcFace
mobilefacenet** → 512-d L2-normalized embedding. Both HEFs in
`/usr/local/hailo/resources/models/hailo8/`.

- **Gallery** at `~/wes/known_faces.json` = `{name: [embeddings]}` (accumulates, keeps
  the most recent 40/person). Match = best cosine vs. all of a person's embeddings;
  threshold **0.45** (same-person ~0.88, so a wide margin).
- **CLI**: `hailo_faces.py enroll <name>` (append samples) / `forget <name>` /
  `recognize` / `scene` (recognize + return the JPEG for Gemma). Enrolled: charlie,
  cindy (add more with varied lighting to strengthen).
- **Clothing color** per face (`_clothing_color`): dominant torso color via cheap
  OpenCV, used as a *disambiguator* so Gemma binds names to the right person when
  people are close together (position alone is too coarse).

## Wake-word vision prefetch (latency hiding)

On wake word the Pi runs `hailo_faces.py scene` → `{name, position, clothing}` + JPEG,
and POSTs to PC `/prefetch_scene` (identities in the `X-Identities` header). The PC
**publishes the identities to the scene cache immediately** (before Gemma finishes),
then runs Gemma in the background with an identity-aware prompt (`_vlm_prompt` — asks
for each named person's appearance/expression/activity, uses clothing colors to keep
names on the right people) and caches the description (`SCENE_TTL=20s`). The capture +
Gemma inference are hidden behind the user's speech. Identity comes from the **Hailo**,
the description from **Gemma**: *"Charlie's in the center wearing yellow, focused on
his tablet with a thoughtful expression."*

## Identity → Claude integration (how person questions work)

- **`_scene_context()`** (wes_server): every Claude turn's system prompt gets a live
  addendum — "currently in frame: charlie (center, wearing orange)" / "N person(s) but
  none match enrolled faces" / "no people" — when face-rec data is fresh (within
  `SCENE_TTL`). It instructs Claude to **treat listed people as present** (answer
  "how does charlie look?" directly) and to say it can't find someone **only** when
  they're not listed. Stale/no data → empty addendum, no behavior change.
- **`describe_scene` tool** returns structured output:
  `{"description", "recognition_ran", "people": [{name, position, clothing}]}`. On a
  cache **miss** it captures via the Pi's **`/scene`** route (face-rec included, new in
  `pi_state.py`) so ad-hoc captures are identity-aware too; falls back to bare
  `/frame` + generic prompt if `/scene` fails.
- Verified live: "How does Charlie look right now?" → cache HIT → *"Charlie's looking
  good — he's in the center of frame wearing yellow, focused on his tablet…"*; asking
  about an enrolled-but-absent person → *"I don't see anyone named Cindy in the
  current view."*
