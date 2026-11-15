"""Entry point.  Run from the repository root:

    python -m server.main

Command-line mode of the server (no window). The exe/GUI is server/gui.py.
"""

from __future__ import annotations

import argparse
import logging
import logging.handlers
import socket
import sys
from pathlib import Path

if not __package__:
    # Started as "python main.py" / "python server\main.py". In that mode the
    # server folder is first on the import path and its `platform` package
    # hides Python's own `platform` module, which breaks libraries in ways
    # that are miserable to debug. Refuse early with the fix.
    sys.exit("Start the server from the repository root with:  python -m server.main")

from aiohttp import web

from . import settings
from .config import DEFAULT_PORT, AccessConfig, StreamConfig
from .net import build_app
from .platform import create_input_sink, create_screen_source

log = logging.getLogger("server")


def _setup_logging(verbose: bool) -> None:
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%H:%M:%S")
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)

    # The windowed exe has no console, so sys.stderr is None there; a stream
    # handler on None would raise on every log line.
    if sys.stderr is not None:
        console = logging.StreamHandler()
        console.setFormatter(fmt)
        root.addHandler(console)

    # File log next to the code, in <repo>/logs. Rotating so a server left
    # running for weeks cannot fill the disk. A failure here (read-only
    # folder) must not stop the server, so it only warns.
    try:
        if getattr(sys, "frozen", False):
            # Running as the PyInstaller exe: __file__ points into a temp
            # folder that is deleted on exit, so log next to the exe instead.
            log_dir = Path(sys.executable).resolve().parent / "logs"
        else:
            log_dir = Path(__file__).resolve().parent.parent / "logs"
        log_dir.mkdir(exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            log_dir / "server.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8"
        )
        file_handler.setFormatter(fmt)
        root.addHandler(file_handler)
    except OSError as exc:
        log.warning("File logging disabled: %s", exc)

    # aiohttp logs one line per HTTP request at INFO; not useful here.
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)


def _lan_ip() -> str:
    """Best guess at this machine's LAN address, for the startup message.

    Trick: "connecting" a UDP socket sends no packets, but it makes the OS
    choose the outgoing network interface, whose address we then read back.
    More reliable than gethostbyname(hostname), which on machines with
    VirtualBox/VPN adapters often returns the wrong interface.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def main() -> None:
    p = argparse.ArgumentParser(description="PhoneDesk server, command-line mode (the exe opens a GUI instead)")
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p.add_argument(
        "--lan",
        action="store_true",
        help="listen on all interfaces so a phone can connect. Without it only "
        "this PC can connect. Phones must send the PIN saved in the settings file.",
    )
    p.add_argument("--view-only", action="store_true", help="stream the screen but ignore all phone input")
    p.add_argument("--width", type=int, default=StreamConfig.max_width, help="max frame width in pixels")
    p.add_argument("--quality", type=int, default=StreamConfig.jpeg_quality, help="JPEG quality 1-95")
    p.add_argument("--fps", type=int, default=StreamConfig.fps, help="target frames per second")
    p.add_argument("--monitor", type=int, default=-1, help="monitor index, -1 = primary")
    p.add_argument(
        "--backend",
        choices=["auto", "dxcam", "mss", "test"],
        default="auto",
        help="capture backend; 'test' streams a synthetic pattern",
    )
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    _setup_logging(args.verbose)

    config = StreamConfig(
        max_width=max(160, args.width),
        jpeg_quality=min(95, max(1, args.quality)),
        fps=min(60, max(1, args.fps)),
        monitor=args.monitor,
    )
    source = create_screen_source(args.backend)
    sink = create_input_sink(args.backend)

    # Same PIN the GUI shows, so CLI and GUI are interchangeable.
    pin = settings.load().pin
    access = AccessConfig(pin=pin if args.lan else "", allow_input=not args.view_only)

    host = "0.0.0.0" if args.lan else "127.0.0.1"
    log.info("Test page on this PC:  http://127.0.0.1:%d/", args.port)
    if args.lan:
        log.info("Phone address: %s  port %d  PIN %s", _lan_ip(), args.port, pin)
    log.info("Press Ctrl+C to stop.")

    # run_app owns the event loop and handles Ctrl+C. print=None silences its
    # own banner, which would duplicate the lines above.
    web.run_app(build_app(config, source, sink, access), host=host, port=args.port, print=None)


if __name__ == "__main__":
    main()
