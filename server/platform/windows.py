"""Windows screen capture: dxcam (fast path) with mss (fallback).

Two capture APIs, because neither is enough alone:

* dxcam uses the DXGI Desktop Duplication API. The GPU hands over the already
  composited desktop image, so a grab costs ~1-3 ms even at 4K, and it tells us
  when nothing changed. But it can fail: some remote sessions, some hybrid-GPU
  laptops, and it loses access whenever the secure desktop (UAC / lock screen)
  appears or the display mode changes.

* mss uses GDI BitBlt. Roughly 10x slower at high resolutions, but it works on
  practically every Windows machine. Since this program gets handed to a friend
  with unknown hardware, a fallback that "just works" matters more than speed.

InputSink (SendInput) is added to this file in milestone 4.
"""

from __future__ import annotations

import ctypes
import logging
from typing import Optional

from .base import Monitor, RawFrame, ScreenSource

log = logging.getLogger(__name__)


def _enable_dpi_awareness() -> None:
    """Tell Windows this process works in physical pixels.

    WHY: without this, on a display set to e.g. 150% scaling Windows lies to
    the process: a 2560x1440 monitor is reported as 1707x960 and screenshots
    come back upscaled and blurry. Per-monitor awareness (value 2) makes monitor
    geometry and captured pixels match the real panel. It must be set before
    any window/DC is created, hence it runs first thing in the constructor.
    """
    try:
        # Windows 8.1+. 2 = PROCESS_PER_MONITOR_DPI_AWARE.
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except (AttributeError, OSError):
        # AttributeError: shcore missing (Windows 7). OSError: awareness was
        # already set (allowed only once per process). Either way, try the
        # older system-wide call and otherwise carry on.
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except (AttributeError, OSError):
            pass


class WindowsScreenSource(ScreenSource):
    def __init__(self, backend: str = "auto") -> None:
        if backend not in ("auto", "dxcam", "mss"):
            raise ValueError(f"Unknown capture backend: {backend}")
        self._backend = backend
        _enable_dpi_awareness()

        # Both handles are created lazily on the capture thread (see the
        # threading contract in base.ScreenSource). mss in particular stores
        # GDI handles per thread and breaks if used from another one.
        self._sct = None            # mss instance
        self._dx = None             # dxcam camera for the primary monitor
        self._dx_tried = False      # only attempt dxcam creation once
        self._dx_has_frame = False  # has dxcam delivered at least one frame?

    # ------------------------------------------------------------------ mss

    def _mss(self):
        if self._sct is None:
            import mss  # imported here so a missing package only matters if used

            self._sct = mss.mss()
        return self._sct

    def list_monitors(self) -> list[Monitor]:
        # WHY mss for enumeration even when dxcam does the capturing: mss gives
        # plain geometry for every monitor through one stable public API.
        # dxcam addresses outputs by (GPU index, output index) and exposes no
        # clean way to map those to desktop coordinates.
        #
        # sct.monitors[0] is the bounding box of ALL monitors combined; the
        # real displays start at index 1.
        monitors = []
        for i, m in enumerate(self._mss().monitors[1:]):
            monitors.append(
                Monitor(
                    index=i,
                    left=m["left"],
                    top=m["top"],
                    width=m["width"],
                    height=m["height"],
                    # Windows defines the primary monitor as the one whose
                    # top-left corner is the desktop origin (0, 0).
                    is_primary=(m["left"] == 0 and m["top"] == 0),
                )
            )
        return monitors

    def _grab_mss(self, monitor: Monitor) -> RawFrame:
        shot = self._mss().grab(
            {
                "left": monitor.left,
                "top": monitor.top,
                "width": monitor.width,
                "height": monitor.height,
            }
        )
        # shot.bgra is the native GDI byte order: Blue, Green, Red, unused.
        return RawFrame(shot.width, shot.height, shot.bgra, "BGRX")

    # ---------------------------------------------------------------- dxcam

    def _dxcam(self):
        """Create the dxcam camera on first use; None if unavailable."""
        if self._dx_tried:
            return self._dx
        self._dx_tried = True
        try:
            import dxcam

            # No output index = the primary output.
            # WHY output_color="BGRA": that is the format Desktop Duplication
            # produces natively. Any other choice makes dxcam convert with
            # OpenCV, which would add a dependency and a full-frame copy that
            # the encoder's own conversion makes redundant.
            self._dx = dxcam.create(output_color="BGRA")
            if self._dx is None:
                raise RuntimeError("dxcam.create() returned None")
            log.info("Capture backend: dxcam (Desktop Duplication)")
        except Exception as exc:  # broad on purpose: any failure means "use mss"
            if self._backend == "dxcam":
                # The user explicitly asked for dxcam; silently substituting
                # mss would make their performance measurements meaningless.
                raise RuntimeError(f"dxcam backend requested but unavailable: {exc}") from exc
            log.warning("dxcam unavailable (%s); falling back to mss", exc)
            self._dx = None
        return self._dx

    def _drop_dxcam(self, reason: object) -> None:
        log.warning("dxcam failed (%s); switching to mss for the rest of this run", reason)
        try:
            self._dx.release()
        except Exception:
            pass
        self._dx = None

    # ----------------------------------------------------------------- grab

    def grab(self, monitor: Monitor) -> Optional[RawFrame]:
        # dxcam path is limited to the primary monitor for now. Mapping the
        # other monitors to dxcam output indices is milestone 7 (multi-monitor);
        # until then secondary monitors go through mss, which is correct but
        # slower.
        use_dx = self._backend != "mss" and monitor.is_primary
        cam = self._dxcam() if use_dx else None

        if cam is not None:
            try:
                arr = cam.grab()
            except Exception as exc:
                # Typical causes: resolution change, UAC prompt, lock screen.
                self._drop_dxcam(exc)
                arr = None
                cam = None

            if arr is not None:
                self._dx_has_frame = True
                # numpy array shaped (height, width, 4). Passed through
                # without copying; the encoder reads it via the buffer protocol.
                return RawFrame(arr.shape[1], arr.shape[0], arr, "BGRX")

            if cam is not None and self._dx_has_frame:
                # dxcam returns None when the desktop has not changed since
                # the last grab. Report "unchanged" so the encoder can reuse
                # its previous JPEG.
                return None
            # else: dxcam has no image yet (static screen at startup) or just
            # failed. The contract says the first grab must return a frame, so
            # fall through and take this one with mss.

        return self._grab_mss(monitor)

    def close(self) -> None:
        if self._dx is not None:
            try:
                self._dx.release()
            except Exception:
                pass
            self._dx = None
        if self._sct is not None:
            self._sct.close()
            self._sct = None
