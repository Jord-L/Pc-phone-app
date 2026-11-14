"""Capture + encode loop.

Platform-agnostic: talks to the screen only through ScreenSource.

Design: ONE dedicated thread grabs, downscales and JPEG-encodes, then hands
each finished frame to a callback. Reasons for a thread rather than an asyncio
task:

1. Grab + resize + encode is 5-30 ms of blocking CPU work. Run on the event
   loop it would stall every WebSocket send and receive for that long.
2. Capture APIs have thread affinity (see ScreenSource docstring); one
   long-lived thread satisfies that trivially.
3. Frame pacing needs sleeps accurate to ~1 ms. asyncio's timers on Windows are
   only accurate to ~15 ms, which at 30 fps (33 ms/frame) is a huge error.
   time.sleep() on Python 3.11+ uses a high-resolution timer.
"""

from __future__ import annotations

import io
import logging
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

from PIL import Image

from .config import StreamConfig
from .platform.base import Monitor, RawFrame, ScreenSource

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class EncodedFrame:
    """A frame ready to go on the wire. Immutable: shared by all clients."""

    frame_id: int
    width: int    # dimensions of the JPEG (after downscale), not of the screen
    height: int
    jpeg: bytes


@dataclass
class CaptureStats:
    """Totals since the previous snapshot; the consumer derives the averages."""

    frames: int = 0
    reused: int = 0         # frames where the screen was unchanged and the JPEG was reused
    grab_ms: float = 0.0    # summed
    encode_ms: float = 0.0  # summed (resize + JPEG)
    jpeg_bytes: int = 0     # summed


def encode_frame(raw: RawFrame, max_width: int, quality: int) -> tuple[bytes, int, int]:
    """Downscale `raw` to at most `max_width` wide and JPEG-encode it.

    Returns (jpeg_bytes, width, height).
    """
    # frombuffer wraps the capture buffer and converts to RGB in one pass.
    # The "raw" decoder arguments are: source byte layout, stride (0 = packed
    # rows), orientation (1 = top row first).
    img = Image.frombuffer("RGB", (raw.width, raw.height), raw.data, "raw", raw.pixel_format, 0, 1)

    if raw.width > max_width:
        # Keep the aspect ratio. Never upscale: that would add bytes without
        # adding information.
        new_size = (max_width, max(1, round(raw.height * max_width / raw.width)))
        # BILINEAR is the cheapest filter that still looks acceptable for
        # text. reducing_gap lets Pillow first shrink by an integer factor
        # with a very fast box filter, then finish with bilinear; about 2-3x
        # faster on large screens for no visible difference at these sizes.
        img = img.resize(new_size, Image.Resampling.BILINEAR, reducing_gap=2.0)

    buf = io.BytesIO()
    # No optimize=True / progressive: both shrink the file a few percent but
    # cost extra encode passes, and here time matters more than bytes.
    img.save(buf, format="JPEG", quality=quality)
    return buf.getvalue(), img.width, img.height


class CaptureThread(threading.Thread):
    """Owns the ScreenSource and produces EncodedFrames at the target fps."""

    def __init__(
        self,
        source: ScreenSource,
        config: StreamConfig,
        on_frame: Callable[[EncodedFrame], None],
    ) -> None:
        # daemon=True: if the main thread dies unexpectedly, this thread must
        # not keep the process alive as a zombie in Task Manager.
        super().__init__(name="capture", daemon=True)
        self._source = source
        self._config = config
        self._on_frame = on_frame  # called ON THIS THREAD; receiver must be thread-safe

        # Set while at least one client is connected. Capturing with nobody
        # watching would burn CPU/GPU for nothing; the server may sit idle in
        # the tray for days.
        self._active = threading.Event()
        # Named _stop_event because threading.Thread itself has a private
        # _stop() method; overwriting it breaks Thread.join().
        self._stop_event = threading.Event()

        # Startup handshake: the network layer needs the monitor list before
        # it can greet clients, and it must learn about startup failures.
        self.ready = threading.Event()
        self.error: Optional[BaseException] = None
        self.monitors: list[Monitor] = []
        self.monitor: Optional[Monitor] = None

        self._stats = CaptureStats()
        self._stats_lock = threading.Lock()

    # ------------------------------------------------- called from other threads

    def set_active(self, active: bool) -> None:
        if active:
            self._active.set()
        else:
            self._active.clear()

    def stop(self) -> None:
        self._stop_event.set()
        self._active.set()  # wake the thread if it is parked waiting for a client

    def take_stats(self) -> CaptureStats:
        """Return the stats accumulated since the last call and reset them."""
        with self._stats_lock:
            stats, self._stats = self._stats, CaptureStats()
        return stats

    # ------------------------------------------------------------ capture thread

    def _pick_monitor(self) -> Monitor:
        wanted = self._config.monitor
        if 0 <= wanted < len(self.monitors):
            return self.monitors[wanted]
        if wanted >= 0:
            log.warning("Monitor %d does not exist; using the primary monitor", wanted)
        # Primary if flagged, else simply the first one.
        return next((m for m in self.monitors if m.is_primary), self.monitors[0])

    def run(self) -> None:
        try:
            self.monitors = self._source.list_monitors()
            if not self.monitors:
                raise RuntimeError("No monitors found")
            self.monitor = self._pick_monitor()
            # One throwaway grab so backend problems (missing package, no
            # desktop access) surface at startup instead of on first connect.
            self._source.grab(self.monitor)
        except BaseException as exc:
            self.error = exc
            return
        finally:
            self.ready.set()

        try:
            self._loop()
        except Exception:
            log.exception("Capture loop crashed")
        finally:
            self._source.close()

    def _loop(self) -> None:
        frame_id = 0
        # Last raw screenshot and its encoded form. The raw one is kept so an
        # unchanged screen can still be re-encoded when the quality settings
        # change (the backend will keep answering "unchanged" and never hand
        # us those pixels again).
        last_raw: Optional[RawFrame] = None
        last: Optional[tuple[bytes, int, int]] = None
        last_settings: Optional[tuple[int, int]] = None
        next_deadline = time.perf_counter()

        while not self._stop_event.is_set():
            if not self._active.is_set():
                self._active.wait()
                # Coming back from idle: the old schedule is long past.
                next_deadline = time.perf_counter()
                continue

            # Read the settings every iteration: the adaptive controller
            # (milestone 3) will change them while this loop runs.
            cfg = self._config
            settings = (cfg.max_width, cfg.jpeg_quality)

            t0 = time.perf_counter()
            raw = self._source.grab(self.monitor)
            t1 = time.perf_counter()

            if raw is not None:
                last_raw = raw
            # Reuse the previous JPEG only if the screen is unchanged AND it
            # was encoded with the settings currently in force.
            reused = raw is None and last is not None and settings == last_settings
            if not reused and last_raw is not None:
                last = encode_frame(last_raw, cfg.max_width, cfg.jpeg_quality)
                last_settings = settings
            t2 = time.perf_counter()

            if last is not None:
                # Even an unchanged screen is re-sent with a new id. That keeps
                # the frame rate (and therefore the latency measurements)
                # steady while tuning. Skipping unchanged frames entirely is a
                # bandwidth optimisation to consider with the adaptive work.
                frame_id += 1
                jpeg, w, h = last
                self._on_frame(EncodedFrame(frame_id, w, h, jpeg))
                with self._stats_lock:
                    s = self._stats
                    s.frames += 1
                    s.reused += int(reused)
                    s.grab_ms += (t1 - t0) * 1000
                    s.encode_ms += (t2 - t1) * 1000
                    s.jpeg_bytes += len(jpeg)

            # Deadline pacing: advance a fixed schedule instead of "sleep
            # 1/fps after each frame". The latter adds the work time on top of
            # the sleep and always runs slower than the target.
            next_deadline += 1.0 / max(1, cfg.fps)
            delay = next_deadline - time.perf_counter()
            if delay > 0:
                # Event.wait instead of time.sleep so stop() interrupts it.
                self._stop_event.wait(delay)
            else:
                # Running behind (encode slower than the frame interval). Do
                # not try to "catch up" with a burst of frames; restart the
                # schedule from now.
                next_deadline = time.perf_counter()
