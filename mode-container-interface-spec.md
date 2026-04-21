# displayrr — Mode Container Interface Specification

## Overview

This document describes how a display mode container integrates with the displayrr system. It is intended for developers building or adapting display mode containers.

Mode containers run on a **Pi 4B or Pi 5** alongside **Switchrr**. They do not have direct access to the physical display — that is owned exclusively by **Indicatrr** on a Pi Zero 2W. All display output happens by publishing rendered frames over the network to Switchrr.

Mode containers also receive hardware input events (encoder rotation, encoder push) from Switchrr when their mode is active.

---

## Migrating an Existing SPI-Based Display Project

If your project currently drives an SPI display directly (e.g. via `luma.oled`, `luma.lcd`, or similar), the changes required are:

1. **Remove all SPI initialisation and direct display writes.** The display is no longer your concern.
2. **Render to a Pillow `Image` object instead of to the device.** Your layout and drawing code is unchanged — only the final output target changes.
3. **Send the rendered image to Switchrr** using the frame publishing interface described below.
4. **Add an event listener** to receive encoder and lifecycle events from Switchrr.
5. **Remove any GPIO reads** for hardware inputs. The encoder and toggle switch are read by Indicatrr; you receive events via Switchrr.

Everything else — data fetching, layout logic, fonts, update scheduling — remains as-is.

---

## System Context

```
Your Container (Pi 4/5)
│
│  PUSH rendered frames ──────────────▶  Switchrr  ──▶  Indicatrr  ──▶  SPI Display
│
│  SUB hardware/lifecycle events  ◀────  Switchrr
```

Switchrr decides whether your frames reach the display based on which mode is active. You do not need to know whether you are currently active — keep producing frames at your normal rate regardless.

---

## Quick-Start Boilerplate

Copy this skeleton as your starting point. It wires up both sockets, starts the event listener, and runs a 25 fps render loop.

```python
import json
import logging
import os
import threading
import time

import zmq
from PIL import Image, ImageDraw

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
logger = logging.getLogger(__name__)

# ── Config from environment ───────────────────────────────────────────────────
FRAME_ADDRESS  = os.environ["SWITCHRR_FRAME_ADDRESS"]
EVENT_ADDRESS  = os.environ["SWITCHRR_EVENT_ADDRESS"]
MODE_NAME      = os.environ["MODE_NAME"]
WIDTH          = int(os.environ.get("DISPLAY_WIDTH",  "256"))
HEIGHT         = int(os.environ.get("DISPLAY_HEIGHT", "64"))

# ── ZMQ setup ─────────────────────────────────────────────────────────────────
ctx = zmq.Context()

frame_socket = ctx.socket(zmq.PUSH)
frame_socket.connect(FRAME_ADDRESS)

event_socket = ctx.socket(zmq.SUB)
event_socket.connect(EVENT_ADDRESS)
event_socket.setsockopt(zmq.SUBSCRIBE, b"")

# ── Frame sender ──────────────────────────────────────────────────────────────
sequence = 0

def send_frame(image):
    global sequence
    pixel_format = "RGB24" if image.mode == "RGB" else "L"
    header = json.dumps({
        "width": image.width,
        "height": image.height,
        "pixel_format": pixel_format,
        "sequence": sequence,
        "timestamp_ms": int(time.time() * 1000),
    }).encode("utf-8")
    try:
        frame_socket.send_multipart([header, image.tobytes()], flags=zmq.NOBLOCK)
    except zmq.Again:
        pass  # HWM reached — drop frame, never block the render loop
    sequence += 1

# ── Event listener ────────────────────────────────────────────────────────────
active = False

def event_loop():
    global active
    while True:
        try:
            msg = json.loads(event_socket.recv())
            event = msg.get("event")
            if event == "MODE_ACTIVE" and msg.get("mode") == MODE_NAME:
                active = True
            elif event == "MODE_INACTIVE" and msg.get("mode") == MODE_NAME:
                active = False
            elif event == "ENCODER_DELTA" and active:
                pass  # handle rotation
            elif event == "ENCODER_PUSH" and active:
                pass  # handle button press
        except Exception as exc:
            logger.warning("event loop error: %s", exc)

threading.Thread(target=event_loop, daemon=True).start()

# ── Render loop ───────────────────────────────────────────────────────────────
TARGET_FPS  = 25
FRAME_TIME  = 1.0 / TARGET_FPS

while True:
    t0 = time.time()

    image = Image.new("L", (WIDTH, HEIGHT), 0)  # black canvas
    draw  = ImageDraw.Draw(image)
    # ... draw your content here ...

    send_frame(image)

    elapsed = time.time() - t0
    if elapsed < FRAME_TIME:
        time.sleep(FRAME_TIME - elapsed)
```

### Using luma.core compositing (viewport / snapshot)

If you want luma's `viewport`/`snapshot` model (automatic sub-region redraw, interval-throttled hotspots), create a thin shim that luma calls `display()` on:

```python
from luma.core.virtual import viewport, snapshot

class _ZMQDevice:
    mode = "L"  # or "RGB" — must match Image.new() mode above

    def __init__(self, width, height):
        self.width  = width
        self.height = height
        self.size   = (width, height)

    def display(self, image):
        send_frame(image)

    def cleanup(self):
        pass

device = _ZMQDevice(WIDTH, HEIGHT)
virtual = viewport(device, width=WIDTH, height=HEIGHT)
# add hotspots, then call virtual.refresh() in your loop instead of send_frame()
```

---

## Connections

Your container makes two outbound ZeroMQ connections. Both addresses are injected via environment variables at startup.

| Connection | Socket type | Environment variable | Direction |
|---|---|---|---|
| Frame output | `PUSH` | `SWITCHRR_FRAME_ADDRESS` | Your container → Switchrr |
| Event input | `SUB` | `SWITCHRR_EVENT_ADDRESS` | Switchrr → your container |

```python
import zmq
import os

context = zmq.Context()

frame_socket = context.socket(zmq.PUSH)
frame_socket.connect(os.environ["SWITCHRR_FRAME_ADDRESS"])

event_socket = context.socket(zmq.SUB)
event_socket.connect(os.environ["SWITCHRR_EVENT_ADDRESS"])
event_socket.setsockopt(zmq.SUBSCRIBE, b"")  # receive all events
```

---

## Publishing Frames

### Message Format

Each send is a **two-part ZeroMQ message**.

**Part 1 — Header (JSON, UTF-8 encoded bytes)**

```json
{
  "width": 256,
  "height": 64,
  "pixel_format": "RGB24",
  "sequence": 1042,
  "timestamp_ms": 1713456789012
}
```

| Field | Type | Description |
|---|---|---|
| `width` | int | Frame width in pixels — must match pixel data |
| `height` | int | Frame height in pixels — must match pixel data |
| `pixel_format` | string | `RGB24` (3 bytes/pixel) or `L` (1 byte/pixel greyscale) |
| `sequence` | int | Increment by 1 per frame, starting from 0 |
| `timestamp_ms` | int | Unix epoch milliseconds at time of render |

**Part 2 — Pixel data (raw bytes)**

Row-major raw pixel bytes. No compression, no encoding, no padding.

- `RGB24`: `width * height * 3` bytes
- `L`: `width * height * 1` bytes

Always size frames using `DISPLAY_WIDTH` and `DISPLAY_HEIGHT` from environment variables. Do not hardcode dimensions.

### Choosing pixel_format

| Format | Bytes/pixel | When to use |
| --- | --- | --- |
| `L` | 1 | Greyscale or monochrome content — preferred for the SSD1322 display |
| `RGB24` | 3 | Only if your content genuinely uses colour |

Prefer `L` — it is ⅓ the bandwidth of `RGB24` and Indicatrr converts to the display's native format either way. Use `Image.new("L", ...)` and `fill=255` for white / `fill=0` for black. The existing departure board uses `fill="yellow"` because it was originally developed against a luma device; this works in `RGB` mode.

### Sending a Frame (Python + Pillow)

```python
import json
import time

sequence = 0

def send_frame(image):
    global sequence
    header = json.dumps({
        "width": image.width,
        "height": image.height,
        "pixel_format": "RGB24" if image.mode == "RGB" else "L",
        "sequence": sequence,
        "timestamp_ms": int(time.time() * 1000)
    }).encode("utf-8")
    frame_socket.send_multipart([header, image.tobytes()])
    sequence += 1
```

If your project already renders to a Pillow `Image`, replace the final device write call with `send_frame(image)`.

### Why NOBLOCK is mandatory

Switchrr's inbound PULL socket has a high-water mark of 5 frames (`FRAME_SEND_HWM=5` in switchrr's `.env`). If you call `send_multipart` without `zmq.NOBLOCK` and Switchrr is momentarily busy, the call will block until buffer space is available — stalling your entire render loop. Always pass `flags=zmq.NOBLOCK` and catch `zmq.Again` silently. A dropped frame at 25 fps is invisible; a stalled render loop is not.

### Frame Rate

| Scenario | Rate |
|---|---|
| Static or data-driven content (e.g. departure boards) | Publish on each data change; minimum 1 fps to signal liveness |
| Animation | Up to 25 fps maximum |

Do not exceed 25 fps — there is no benefit given the display hardware and network path.

**Keep publishing frames even when your mode is not active.** Switchrr discards them from inactive modes, but this ensures zero latency when your mode is selected. If your container makes expensive external calls (API fetches, MQTT subscriptions etc.), you may slow to 1 fps when inactive — but do not stop entirely.

---

## Receiving Events

Switchrr publishes all hardware and lifecycle events to all connected mode containers. You receive everything and filter for what is relevant.

### Event Format

Single ZeroMQ frame — JSON, UTF-8.

```json
{ "event": "ENCODER_DELTA", "delta": 1, "timestamp_ms": 1713456789200 }
{ "event": "ENCODER_PUSH", "timestamp_ms": 1713456789300 }
{ "event": "MODE_ACTIVE", "mode": "uk_tdd", "timestamp_ms": 1713456789400 }
{ "event": "MODE_INACTIVE", "mode": "uk_tdd", "timestamp_ms": 1713456789500 }
```

### Event Reference

| `event` | Fields | Description |
|---|---|---|
| `ENCODER_DELTA` | `delta: int` | Encoder rotated. `+1` per clockwise detent, `-1` anticlockwise. You decide what this means |
| `ENCODER_PUSH` | — | Encoder button released |
| `MODE_ACTIVE` | `mode: string` | Named mode became active |
| `MODE_INACTIVE` | `mode: string` | Named mode became inactive |

`TOGGLE_SWITCH` events are not published to mode containers — they are consumed internally by Switchrr.

### Tracking Active State

Track your own active/inactive state using `MODE_ACTIVE` and `MODE_INACTIVE`. Only respond to encoder events when active.

```python
import threading
import json

MY_MODE_NAME = os.environ["MODE_NAME"]
active = False

def event_loop():
    global active
    while True:
        msg = json.loads(event_socket.recv())
        event = msg.get("event")

        if event == "MODE_ACTIVE" and msg.get("mode") == MY_MODE_NAME:
            active = True
            on_activated()
        elif event == "MODE_INACTIVE" and msg.get("mode") == MY_MODE_NAME:
            active = False
            on_deactivated()
        elif event == "ENCODER_DELTA" and active:
            handle_encoder_delta(msg["delta"])
        elif event == "ENCODER_PUSH" and active:
            handle_encoder_push()

threading.Thread(target=event_loop, daemon=True).start()
```

`on_activated()` and `on_deactivated()` are optional hooks — useful if you want to resume full frame rate on activation or throttle expensive operations on deactivation.

### What Switchrr displays before your first frame

When your container starts, Switchrr holds the last frame it received from any source for your mode. On a fresh start with no prior state, the display shows black. Push a frame as early as possible — even a blank one — so the display is not left showing stale content from a previous run.

---

## Environment Variables

Provided by Docker Compose at startup:

| Variable | Description |
|---|---|
| `SWITCHRR_FRAME_ADDRESS` | ZeroMQ address to PUSH frames to |
| `SWITCHRR_EVENT_ADDRESS` | ZeroMQ address to SUB events from |
| `MODE_NAME` | Your mode's unique identifier — must match the name registered in Switchrr's `MODE_REGISTRY` |
| `DISPLAY_WIDTH` | Width of the target display in pixels |
| `DISPLAY_HEIGHT` | Height of the target display in pixels |

---

## Packaging

- Run as a Docker container on the Pi 4/5
- Expose no ports — all communication is outbound via ZeroMQ
- No `--privileged` flag required — no hardware access
- Include `pyzmq` in dependencies
- Use `DISPLAY_WIDTH` / `DISPLAY_HEIGHT` at runtime, not build time

---

## Local Development Without Switchrr

The PUSH socket connects silently and ZMQ drops frames into the void if nothing is receiving. To verify your container is sending correctly without needing Switchrr running, use this one-file receiver:

```python
# recv_test.py — run on the same machine as your container
import json
import zmq

ctx = zmq.Context()
sock = ctx.socket(zmq.PULL)
sock.bind("tcp://0.0.0.0:5600")
print("Listening on :5600 ...")

while True:
    header_bytes, pixel_bytes = sock.recv_multipart()
    header = json.loads(header_bytes)
    expected = header["width"] * header["height"] * (3 if header["pixel_format"] == "RGB24" else 1)
    print(f"seq={header['sequence']:6d}  {header['pixel_format']}  "
          f"{header['width']}x{header['height']}  "
          f"{len(pixel_bytes)} bytes (expected {expected})")
```

Run it with `python3 recv_test.py` before starting your container. You should see one line per frame at your target rate.

---

## What Not To Do

| Don't | Why |
|---|---|
| Initialise or write to any SPI device | The display is on the Pi Zero 2W and is not accessible from this machine |
| Read GPIO for encoder or switch inputs | These are owned by Indicatrr. You receive events via Switchrr |
| Stop publishing frames entirely when inactive | Causes visible latency on mode switch |
| Hardcode `MODE_NAME` | Read it from the environment variable — this is how Switchrr identifies you |
| Act on `ENCODER_DELTA` or `ENCODER_PUSH` when inactive | The user is interacting with a different mode |

---

## Responsibility Summary

| Concern | Owner |
|---|---|
| Rendering pixels | Your container |
| Fetching and processing data | Your container |
| Deciding what encoder input means | Your container |
| Publishing frames | Your container |
| Writing to the SPI display | Indicatrr (Pi Zero 2W) |
| Reading physical hardware inputs | Indicatrr (Pi Zero 2W) |
| Routing frames to the active mode | Switchrr |
| Routing hardware events to containers | Switchrr |
| Determining which mode is active | Switchrr |
| Lifecycle notifications | Switchrr |
