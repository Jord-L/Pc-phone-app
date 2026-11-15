"""Launcher used by PyInstaller (and usable directly: python run_server.py).

Default: open the PhoneDesk window (what double-clicking the exe does).
With --cli: run the headless command-line server instead, e.g.
    python run_server.py --cli --lan

WHY a separate file: PyInstaller needs a plain script as its entry point, but
the server code must be imported as part of the `server` package (see the
`platform` naming note in server/platform/__init__.py). This script lives at
the repo root, so importing from here keeps the package intact.
"""

import os
import sys

# The windowed exe (built without a console) has no stdout/stderr: both are
# None, and any library that print()s (dxcam does) would crash. Point them at
# the null device so stray prints vanish harmlessly.
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w")

if __name__ == "__main__":
    if "--cli" in sys.argv:
        sys.argv.remove("--cli")
        from server.main import main
    else:
        from server.gui import main
    main()
