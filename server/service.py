"""Runs the network server in a background thread so a GUI can switch it on and off.

WHY a thread with its own event loop: tkinter's main loop must own the main
thread, and aiohttp needs an asyncio loop. Giving the server a private loop in
a worker thread means the window never freezes while frames stream, and
"stop" is a clean shutdown (capture released, port freed) instead of killing
the process. The same controller restarts the server after a settings change.

The GUI reads `state`, `error` and `client_count()` by polling. That avoids
touching tkinter from this thread, which is not allowed.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Optional

from aiohttp import web

from .config import AccessConfig
from .net import StreamHub, build_app
from .platform import create_input_sink, create_screen_source
from .settings import Settings

log = logging.getLogger(__name__)

STOPPED, STARTING, RUNNING, STOPPING, FAILED = "stopped", "starting", "running", "stopping", "failed"


class ServiceController:
    def __init__(self) -> None:
        self.state = STOPPED
        self.error: Optional[str] = None
        self._thread: Optional[threading.Thread] = None
        # A threading.Event (not an asyncio one) so stop() can be called from
        # any thread at any moment, even before the loop exists.
        self._stop_requested = threading.Event()
        self._hub: Optional[StreamHub] = None

    # ------------------------------------------------------------ GUI-facing

    @property
    def active(self) -> bool:
        """True while a server thread exists (starting, running or stopping)."""
        return self._thread is not None and self._thread.is_alive()

    def client_count(self) -> int:
        hub = self._hub
        return len(hub.clients) if hub is not None else 0

    def start(self, settings: Settings) -> None:
        if self.active:
            return
        self.error = None
        self.state = STARTING
        self._stop_requested.clear()
        self._thread = threading.Thread(target=self._run, args=(settings,), name="server", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self.active:
            self.state = STOPPING
            self._stop_requested.set()

    def join(self, timeout: float = 5.0) -> None:
        if self._thread is not None:
            self._thread.join(timeout)

    # ----------------------------------------------------------- server thread

    def _run(self, settings: Settings) -> None:
        try:
            asyncio.run(self._serve(settings))
            self.state = STOPPED
        except OSError as exc:
            # Most likely "address already in use": another program (or a
            # second PhoneDesk) holds the port.
            log.error("Server could not start: %s", exc)
            if exc.errno in (10048, 98, 48):  # Windows / Linux / macOS "address in use"
                self.error = f"Port {settings.port} is already in use by another program. Pick another port."
            else:
                self.error = f"Cannot listen on port {settings.port}: {exc.strerror or exc}"
            self.state = FAILED
        except Exception as exc:  # capture backend failure, etc.
            log.exception("Server crashed")
            self.error = str(exc)
            self.state = FAILED
        finally:
            self._hub = None

    async def _serve(self, s: Settings) -> None:
        access = AccessConfig(pin=s.pin if s.lan else "", allow_input=s.allow_input)
        app = build_app(
            s.stream_config(),
            create_screen_source(s.backend),
            create_input_sink(s.backend),
            access,
        )
        runner = web.AppRunner(app)
        # setup() runs the app's startup code, which starts screen capture and
        # raises if it cannot, so a broken backend fails here and not later.
        await runner.setup()
        try:
            host = "0.0.0.0" if s.lan else "127.0.0.1"
            await web.TCPSite(runner, host, s.port).start()
            self._hub = app["hub"]
            if self._stop_requested.is_set():
                return
            self.state = RUNNING
            log.info("Sharing ON (%s:%d, input %s)", host, s.port, "allowed" if s.allow_input else "view-only")
            while not self._stop_requested.is_set():
                await asyncio.sleep(0.2)
        finally:
            await runner.cleanup()
            log.info("Sharing OFF")
