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

WindowsInputSink (cursor + SendInput) is at the bottom of this file.
"""

from __future__ import annotations

import ctypes
import logging
from ctypes import wintypes
from typing import Optional

from .base import InputSink, Monitor, RawFrame, ScreenSource

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


# =========================================================================
# Input injection
# =========================================================================

# A private WinDLL instead of ctypes.windll.user32: setting argtypes/restype
# on the shared windll object would change the signatures other libraries in
# this process (dxcam, comtypes, mss) see. use_last_error lets us report why a
# call failed.
_user32 = ctypes.WinDLL("user32", use_last_error=True)

_INPUT_MOUSE = 0
_INPUT_KEYBOARD = 1

_MOUSEEVENTF_LEFTDOWN = 0x0002
_MOUSEEVENTF_LEFTUP = 0x0004
_MOUSEEVENTF_RIGHTDOWN = 0x0008
_MOUSEEVENTF_RIGHTUP = 0x0010
_MOUSEEVENTF_MIDDLEDOWN = 0x0020
_MOUSEEVENTF_MIDDLEUP = 0x0040
_MOUSEEVENTF_WHEEL = 0x0800
_MOUSEEVENTF_HWHEEL = 0x1000

_KEYEVENTF_EXTENDEDKEY = 0x0001
_KEYEVENTF_KEYUP = 0x0002
_KEYEVENTF_UNICODE = 0x0004

_WHEEL_DELTA = 120  # one wheel "notch"

_SM_XVIRTUALSCREEN, _SM_YVIRTUALSCREEN, _SM_CXVIRTUALSCREEN, _SM_CYVIRTUALSCREEN = 76, 77, 78, 79

_BUTTON_FLAGS = {
    "left": (_MOUSEEVENTF_LEFTDOWN, _MOUSEEVENTF_LEFTUP),
    "right": (_MOUSEEVENTF_RIGHTDOWN, _MOUSEEVENTF_RIGHTUP),
    "middle": (_MOUSEEVENTF_MIDDLEDOWN, _MOUSEEVENTF_MIDDLEUP),
}

# name -> (virtual-key code, is "extended" key). Extended keys (arrows, Home,
# End, ...) must carry KEYEVENTF_EXTENDEDKEY or Windows treats them as the
# numpad variants, which do nothing when NumLock is on.
_KEYS = {
    "enter": (0x0D, False), "backspace": (0x08, False), "tab": (0x09, False),
    "escape": (0x1B, False), "space": (0x20, False),
    "delete": (0x2E, True), "insert": (0x2D, True),
    "left": (0x25, True), "up": (0x26, True), "right": (0x27, True), "down": (0x28, True),
    "home": (0x24, True), "end": (0x23, True), "pageup": (0x21, True), "pagedown": (0x22, True),
    "ctrl": (0x11, False), "alt": (0x12, False), "shift": (0x10, False), "win": (0x5B, True),
}
# Letters and digits: the virtual-key code equals the ASCII code of the
# UPPERCASE letter / the digit. These are what shortcuts (Ctrl+C, Alt+4...)
# need: type_text() injects characters, which Windows does not treat as keys.
for _c in "abcdefghijklmnopqrstuvwxyz0123456789":
    _KEYS[_c] = (ord(_c.upper()), False)
# F1..F12 are 0x70..0x7B.
for _n in range(1, 13):
    _KEYS[f"f{_n}"] = (0x70 + _n - 1, False)


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t),
    ]


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t),
    ]


class _INPUT(ctypes.Structure):
    class _U(ctypes.Union):
        _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT)]

    # Union padded to the real size by the compiler; with MOUSEINPUT as the
    # largest member sizeof(INPUT) is 40 on x64, which is what SendInput checks.
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _U)]


_user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(_INPUT), ctypes.c_int]
_user32.SendInput.restype = wintypes.UINT
_user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
_user32.SetCursorPos.restype = wintypes.BOOL
_user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
_user32.GetCursorPos.restype = wintypes.BOOL
_user32.GetSystemMetrics.argtypes = [ctypes.c_int]
_user32.GetSystemMetrics.restype = ctypes.c_int


def _send(*inputs: _INPUT) -> None:
    arr = (_INPUT * len(inputs))(*inputs)
    sent = _user32.SendInput(len(inputs), arr, ctypes.sizeof(_INPUT))
    if sent != len(inputs):
        # Typical cause: the foreground window runs elevated (UIPI blocks
        # input from a non-elevated process). Log once per failure, don't crash.
        log.warning("SendInput injected %d of %d events (error %d)", sent, len(inputs), ctypes.get_last_error())


def _mouse_event(flags: int, data: int = 0) -> _INPUT:
    i = _INPUT(type=_INPUT_MOUSE)
    i.mi = _MOUSEINPUT(0, 0, data & 0xFFFFFFFF, flags, 0, 0)
    return i


def _key_event(vk: int, scan: int, flags: int) -> _INPUT:
    i = _INPUT(type=_INPUT_KEYBOARD)
    i.ki = _KEYBDINPUT(vk, scan, flags, 0, 0)
    return i


class WindowsInputSink(InputSink):
    def __init__(self) -> None:
        # Cursor coordinates are only physical pixels when the process is DPI
        # aware; otherwise SetCursorPos would be off by the display scale.
        _enable_dpi_awareness()

    # -- cursor ---------------------------------------------------------------
    # WHY SetCursorPos/GetCursorPos instead of SendInput MOUSEMOVE: relative
    # SendInput moves go through the user's pointer-acceleration curve, so the
    # cursor would land somewhere different from where the phone asked, and
    # absolute SendInput positions are rounded onto a 0..65535 grid (up to 1px
    # off on big desktops). SetCursorPos is exact and still triggers hover.

    def _desktop(self) -> tuple[int, int, int, int]:
        gm = _user32.GetSystemMetrics
        return gm(_SM_XVIRTUALSCREEN), gm(_SM_YVIRTUALSCREEN), gm(_SM_CXVIRTUALSCREEN), gm(_SM_CYVIRTUALSCREEN)

    def mouse_move_to(self, x: int, y: int) -> None:
        left, top, w, h = self._desktop()
        x = min(max(x, left), left + w - 1)
        y = min(max(y, top), top + h - 1)
        _user32.SetCursorPos(x, y)

    def cursor_pos(self) -> Optional[tuple[int, int]]:
        p = wintypes.POINT()
        return (p.x, p.y) if _user32.GetCursorPos(ctypes.byref(p)) else None

    def mouse_move_rel(self, dx: int, dy: int) -> None:
        pos = self.cursor_pos()
        if pos is not None:
            self.mouse_move_to(pos[0] + dx, pos[1] + dy)

    # -- buttons / wheel ------------------------------------------------------

    def mouse_button(self, btn: str, down: bool) -> None:
        flags = _BUTTON_FLAGS.get(btn)
        if flags is None:
            return
        _send(_mouse_event(flags[0] if down else flags[1]))

    def scroll(self, dx: float, dy: float) -> None:
        events = []
        if dy:
            # Windows wheel data is positive when the wheel turns away from the
            # user, which scrolls the page UP. Our contract says dy > 0 scrolls
            # DOWN, hence the minus sign.
            events.append(_mouse_event(_MOUSEEVENTF_WHEEL, int(-dy * _WHEEL_DELTA)))
        if dx:
            events.append(_mouse_event(_MOUSEEVENTF_HWHEEL, int(dx * _WHEEL_DELTA)))
        if events:
            _send(*events)

    # -- keyboard -------------------------------------------------------------

    def key(self, name: str, down: bool) -> None:
        entry = _KEYS.get(name)
        if entry is None:
            return
        vk, extended = entry
        flags = (_KEYEVENTF_EXTENDEDKEY if extended else 0) | (0 if down else _KEYEVENTF_KEYUP)
        _send(_key_event(vk, 0, flags))

    def type_text(self, text: str) -> None:
        events = []
        # KEYEVENTF_UNICODE injects a UTF-16 code unit directly, so any
        # character types correctly whatever keyboard layout is active.
        # Characters outside the BMP become a surrogate pair = two units,
        # which is exactly how the API expects them.
        data = text.encode("utf-16-le")
        for i in range(0, len(data), 2):
            unit = int.from_bytes(data[i:i + 2], "little")
            if unit == 0x0A:  # "\n" -> a real Enter key press
                events.append(_key_event(0x0D, 0, 0))
                events.append(_key_event(0x0D, 0, _KEYEVENTF_KEYUP))
                continue
            events.append(_key_event(0, unit, _KEYEVENTF_UNICODE))
            events.append(_key_event(0, unit, _KEYEVENTF_UNICODE | _KEYEVENTF_KEYUP))
        if events:
            _send(*events)


# =========================================================================
# Process helpers
# =========================================================================

def prepare_process() -> None:
    """Call before creating any window (see _enable_dpi_awareness)."""
    _enable_dpi_awareness()


_instance_mutex = None  # kept referenced for the life of the process


def acquire_single_instance(name: str = "PhoneDesk.SingleInstance") -> bool:
    """True if this is the only running copy.

    WHY: two copies would fight over the port and two tray icons would confuse
    the user. A named mutex is released by Windows automatically if we crash,
    so unlike a lock file it can never be left behind stale.
    """
    global _instance_mutex
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    k32.CreateMutexW.restype = wintypes.HANDLE
    _instance_mutex = k32.CreateMutexW(None, False, name)
    return ctypes.get_last_error() != 183  # ERROR_ALREADY_EXISTS
