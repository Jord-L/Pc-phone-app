"""Platform abstraction: the ONLY contract the rest of the server knows about.

WHY this exists: capture and input injection are the two things that differ
completely between Windows, X11 and Wayland. Everything else (encoding,
networking, adaptive quality) is identical. If those modules only ever talk to
these two interfaces, adding Linux later means writing one new file instead of
touching the whole server.

Rule: nothing outside server/platform/ may import an OS-specific module.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Optional


@dataclass(frozen=True)
class Monitor:
    """One physical display, in desktop (virtual screen) pixel coordinates."""

    index: int        # position in list_monitors(); what the client sends back to select it
    left: int         # can be negative: monitors left of/above the primary have negative coords
    top: int
    width: int        # physical pixels (not DPI-scaled "logical" pixels)
    height: int
    is_primary: bool


@dataclass
class RawFrame:
    """An uncompressed screenshot.

    WHY not just return a PIL image or numpy array: each capture backend has a
    different native output (dxcam -> numpy BGRA, mss -> bytes BGRA). Converting
    inside the backend would cost a full-frame copy that the encoder then has to
    repeat. Instead the backend hands over its native buffer plus a tag saying
    how the bytes are laid out, and the encoder does a single conversion.
    """

    width: int
    height: int
    data: Any           # any object supporting the buffer protocol (bytes, bytearray, numpy array)
    pixel_format: str   # "RGB" (3 bytes/pixel) or "BGRX" (4 bytes/pixel, 4th byte ignored)


class ScreenSource(ABC):
    """Produces raw screenshots.

    Threading contract: every method is called from ONE dedicated capture
    thread. Capture APIs tend to have thread affinity (GDI device contexts,
    COM objects), so implementations should create their OS handles lazily
    inside these methods rather than in __init__, which runs on the main thread.
    """

    @abstractmethod
    def list_monitors(self) -> list[Monitor]:
        """Return all displays. Exactly one should have is_primary=True."""

    @abstractmethod
    def grab(self, monitor: Monitor) -> Optional[RawFrame]:
        """Capture one frame of `monitor`.

        Returns None when the backend knows the screen has not changed since
        the previous grab (Desktop Duplication reports this for free). The
        caller can then reuse its last encoded frame and skip the expensive
        resize + JPEG work. The first call must always return a frame.
        """

    def close(self) -> None:
        """Release OS resources. Default: nothing to release."""


class InputSink(ABC):
    """Injects mouse and keyboard input. Implemented in milestone 4/5.

    Defined now so the interface shape is fixed before any platform code is
    written against it.
    """

    @abstractmethod
    def mouse_move_rel(self, dx: int, dy: int) -> None: ...

    @abstractmethod
    def mouse_button(self, btn: str, down: bool) -> None: ...

    @abstractmethod
    def scroll(self, dx: int, dy: int) -> None: ...

    @abstractmethod
    def key(self, name: str, down: bool) -> None: ...

    @abstractmethod
    def type_text(self, text: str) -> None: ...
