# Peripherals & Hardware Details

Connected and planned peripherals for WES.

## Camera

### Logitech C920 PRO HD Webcam

**Connection:** USB 2.0 (Bus 001, Device 002)  
**ID:** 046d:08e5 (Logitech vendor ID)  
**Serial:** F413F23F  
**Driver:** uvcvideo (Linux native, kernel 6.12.62)  
**Device files:** `/dev/video0`, `/dev/video1`, `/dev/media3`

#### Supported Formats

**YUYV (Uncompressed 4:2:2)**
- 640×480 @ 30 fps
- 800×600 @ 24 fps
- 1280×720 @ 7.5 fps (uncompressed)
- 1920×1080 @ 5 fps
- 2560×1472 @ 2 fps
- Full resolution range: 160×90 to 2560×1472

**MJPEG (Motion-JPEG, Compressed)**
- Same resolutions as YUYV
- Better compression (lower bandwidth)
- 1280×720 @ 30 fps possible with MJPEG
- 1920×1080 @ 30 fps possible with MJPEG

#### Use Cases

- **Real-time interaction (30 fps):** Use MJPEG at 640×480 or 800×600, or YUYV at lower res
- **Moderate latency (7.5 fps):** 1280×720 YUYV or 1280×720 MJPEG
- **High-res processing:** 1920×1080 @ 30 fps with MJPEG compression
- **AI inference:** Feed lower res (480p-720p) to Hailo-8L on AI Hat+

#### Notes

- Supports auto-focus and white balance via UVC controls
- No proprietary drivers required
- USB 2.0 bandwidth sufficient for 1080p MJPEG or 720p YUYV

## Microphone

**Status:** Integrated in Logitech C920 PRO HD Webcam  
**Device files:** Check `arecord -l` for ALSA device numbering  
**Use:** Speech-to-text (STT) input

## Speaker

**Status:** Not yet connected  
**Required for:** Text-to-speech (TTS) output

## Raspberry Pi AI Hat+ (Hailo-8L)

- **Inference capability:** ~13 TOPS
- **Firmware version:** 4.20.0
- **Driver status:** Loaded (`/dev/hailo0`)
- **Use case:** On-device preprocessing (noise detection, face detection, activity classification)

## GPIO & USB Expansion

**Planned for:** Peripheral control (LEDs, buttons, relays, etc.)
