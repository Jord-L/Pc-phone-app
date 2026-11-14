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

from .base import InputSink, Monitor, RawFrame, ScreenSource

__all__ = ["InputSink", "Monitor", "RawFrame", "ScreenSource", "create_screen_source"]


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
