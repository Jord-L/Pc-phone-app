"""Launcher used by PyInstaller (and usable directly: python run_server.py).

WHY a separate file: PyInstaller needs a plain script as its entry point, but
server/main.py must be imported as part of the `server` package (see the
`platform` naming note in server/platform/__init__.py). This script lives at
the repo root, so importing `server.main` from here keeps the package intact.
"""

from server.main import main

if __name__ == "__main__":
    main()
