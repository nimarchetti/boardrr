"""
output.py — configurable display output backend.

Reads OUTPUT_MODE from the environment:
  "zmq"  → creates a ZMQ PUSH socket and returns a _ZMQDevice shim that
            luma's viewport can call .display(image) on.
  "spi"  → delegates to helpers.get_device(), which reads luma CLI args
            from sys.argv. Requires luma.oled and SPI hardware packages.

The returned object (real luma device or _ZMQDevice) must satisfy:
  .mode    — PIL image mode string, e.g. "RGB"
  .width   — int
  .height  — int
  .size    — (width, height) tuple
  .display(image) — called by viewport.refresh() on every frame
"""

import json
import logging
import os
import threading
import time

logger = logging.getLogger(__name__)


class _ZMQDevice:
    """
    Minimal luma-compatible device shim that pushes rendered PIL images
    to a ZeroMQ PUSH socket as two-part messages (JSON header + raw pixels).

    Attributes required by luma.core.virtual.viewport:
      .mode   — PIL image mode ("RGB")
      .width  — display width in pixels
      .height — display height in pixels
      .size   — (width, height) tuple

    Additional attribute (not used by luma):
      .encoder_queue — queue.Queue of encoder event dicts, or None in SPI mode
    """

    mode = "RGB"

    def __init__(self, width, height, frame_socket, mode_name, encoder_queue=None):
        self.width = width
        self.height = height
        self.size = (width, height)
        self._frame_socket = frame_socket
        self._mode_name = mode_name
        self._sequence = 0
        self.encoder_queue = encoder_queue

    def display(self, image):
        """
        Called by luma's viewport.refresh() with a PIL Image of size
        (self.width, self.height) and mode self.mode.

        Sends a two-part ZMQ message:
          Part 1: JSON header (UTF-8 bytes)
          Part 2: raw pixel bytes (RGB24: width * height * 3 bytes)
        """
        import zmq
        pixel_format = "RGB24" if image.mode == "RGB" else "L"
        header = json.dumps({
            "width": image.width,
            "height": image.height,
            "pixel_format": pixel_format,
            "sequence": self._sequence,
            "timestamp_ms": int(time.time() * 1000),
        }).encode("utf-8")
        try:
            self._frame_socket.send_multipart([header, image.tobytes()], flags=zmq.NOBLOCK)
        except Exception as exc:
            # Swallow ZMQ EAGAIN (HWM reached) — frame drop at full buffer is normal
            logger.debug("ZMQ send skipped: %s", exc)
        self._sequence += 1

    def cleanup(self):
        pass


def _start_event_listener(event_socket, mode_name, encoder_queue):
    """
    Daemon thread: subscribes to Switchrr event PUB socket.
    - Tracks MODE_ACTIVE / MODE_INACTIVE for this mode.
    - When active, routes ENCODER_DELTA and ENCODER_PUSH events onto encoder_queue.
    """
    def _loop():
        active = False
        logger.info("ZMQ event listener started (mode=%s)", mode_name)
        while True:
            try:
                msg = json.loads(event_socket.recv())
                event = msg.get("event")
                if event == "MODE_ACTIVE" and msg.get("mode") == mode_name:
                    active = True
                    logger.info("lifecycle event: MODE_ACTIVE")
                elif event == "MODE_INACTIVE" and msg.get("mode") == mode_name:
                    active = False
                    logger.info("lifecycle event: MODE_INACTIVE")
                elif active and event in ("ENCODER_DELTA", "ENCODER_PUSH"):
                    encoder_queue.put(msg)
            except Exception as exc:
                logger.warning("ZMQ event listener error: %s", exc)

    threading.Thread(target=_loop, daemon=True, name="zmq-event-listener").start()


def _create_zmq_device():
    """
    Set up ZMQ PUSH + SUB sockets and return a _ZMQDevice shim.

    Required environment variables:
      SWITCHRR_FRAME_ADDRESS  — ZMQ address to PUSH frames to
      SWITCHRR_EVENT_ADDRESS  — ZMQ address to SUB events from
      MODE_NAME               — this container's mode identifier
      DISPLAY_WIDTH           — display width in pixels (default 256)
      DISPLAY_HEIGHT          — display height in pixels (default 64)
    """
    import queue as _queue
    import zmq

    frame_address = os.environ["SWITCHRR_FRAME_ADDRESS"]
    event_address = os.environ["SWITCHRR_EVENT_ADDRESS"]
    mode_name = os.environ["MODE_NAME"]
    width = int(os.environ.get("DISPLAY_WIDTH", "256"))
    height = int(os.environ.get("DISPLAY_HEIGHT", "64"))

    context = zmq.Context()

    frame_socket = context.socket(zmq.PUSH)
    frame_socket.connect(frame_address)
    logger.info("ZMQ PUSH connected to %s", frame_address)

    event_socket = context.socket(zmq.SUB)
    event_socket.connect(event_address)
    event_socket.setsockopt(zmq.SUBSCRIBE, b"")
    logger.info("ZMQ SUB connected to %s", event_address)

    encoder_queue = _queue.Queue()
    _start_event_listener(event_socket, mode_name, encoder_queue)

    return _ZMQDevice(width=width, height=height, frame_socket=frame_socket,
                      mode_name=mode_name, encoder_queue=encoder_queue)


def create_output():
    """
    Factory: returns either a real luma device (SPI mode) or a _ZMQDevice
    shim (ZMQ mode) based on the OUTPUT_MODE environment variable.

    OUTPUT_MODE="spi"  — calls helpers.get_device(), requires luma.oled
    OUTPUT_MODE="zmq"  — creates ZMQ sockets, returns _ZMQDevice
    OUTPUT_MODE absent — defaults to "zmq"

    Raises SystemExit with a clear message on misconfiguration.
    """
    output_mode = os.environ.get("OUTPUT_MODE", "zmq").lower().strip()

    if output_mode == "spi":
        try:
            from helpers import get_device
        except ImportError as exc:
            raise SystemExit(
                f"OUTPUT_MODE=spi but luma/SPI packages are not installed: {exc}\n"
                "Install requirements-spi.txt for SPI hardware mode."
            ) from exc
        logger.info("output mode: SPI (luma device)")
        return get_device()

    if output_mode == "zmq":
        try:
            import zmq  # noqa: F401
        except ImportError as exc:
            raise SystemExit(
                f"OUTPUT_MODE=zmq but pyzmq is not installed: {exc}"
            ) from exc
        for var in ("SWITCHRR_FRAME_ADDRESS", "SWITCHRR_EVENT_ADDRESS", "MODE_NAME"):
            if not os.environ.get(var):
                raise SystemExit(
                    f"OUTPUT_MODE=zmq requires environment variable {var!r} to be set."
                )
        logger.info("output mode: ZMQ")
        return _create_zmq_device()

    raise SystemExit(
        f"Unknown OUTPUT_MODE={output_mode!r}. Valid values: 'spi', 'zmq'."
    )
