"""Picks the platform implementation at startup.

WHY the imports are inside the function: `windows.py` imports ctypes.windll,
dxcam, etc., which do not exist on Linux. Importing lazily means merely loading
this package never fails on the "wrong" OS; only asking for a backend that the
current OS cannot provide does.

NOTE on the package name: this package is called `platform`, same as a module
in Python's standard library. That is safe ONLY because the server is always
started as a package (`python -m server.main`), which makes this
`server.platform`. Running `python main.py` from inside the server folder would
put that folder first on the import path and this package would shadow the
stdlib module, breaking third-party libraries in confusing ways. main.py
guards against that.
"""

from __future__ import annotations

import sys

from .base import InputSink, Monitor, NullInputSink, RawFrame, ScreenSource

__all__ = [
    "InputSink",
    "Monitor",
    "NullInputSink",
    "RawFrame",
    "ScreenSource",
    "acquire_single_instance",
    "create_input_sink",
    "create_screen_source",
    "list_monitors",
    "prepare_process",
]


def create_screen_source(backend: str = "auto") -> ScreenSource:
    """Return the ScreenSource for this OS.

    backend:
      "auto"  - best available for the OS (Windows: dxcam, falling back to mss)
      "dxcam" - Windows Desktop Duplication only
      "mss"   - GDI capture only (slower, but works almost everywhere)
      "test"  - synthetic moving pattern, no real screen needed
    """
    if backend == "test":
        from .synthetic import SyntheticScreenSource

        return SyntheticScreenSource()

    if sys.platform == "win32":
        from .windows import WindowsScreenSource

        return WindowsScreenSource(backend)

    raise NotImplementedError(
        f"No screen capture implementation for platform '{sys.platform}' yet. "
        "Use --backend test to run the pipeline with a synthetic image."
    )


def create_input_sink(backend: str = "auto") -> InputSink:
    """Return the InputSink for this OS. The synthetic backend gets a no-op
    sink so tests of the protocol never move the real mouse."""
    if backend == "test":
        return NullInputSink()

    if sys.platform == "win32":
        from .windows import WindowsInputSink

        return WindowsInputSink()

    raise NotImplementedError(f"No input injection for platform '{sys.platform}' yet.")


def list_monitors(backend: str = "auto") -> list[Monitor]:
    """Monitor list for the GUI's picker.

    Uses a throwaway source created and closed on the CALLING thread, because
    capture handles must stay on the thread that made them.
    """
    source = create_screen_source(backend)
    try:
        return source.list_monitors()
    finally:
        source.close()


def prepare_process() -> None:
    """Per-process setup that must happen before any window exists
    (Windows: DPI awareness, so capture and cursor use physical pixels)."""
    if sys.platform == "win32":
        from .windows import prepare_process as _prepare

        _prepare()


def acquire_single_instance() -> bool:
    """True if no other PhoneDesk is running (always True where unsupported)."""
    if sys.platform == "win32":
        from .windows import acquire_single_instance as _acquire

        return _acquire()
    return True
