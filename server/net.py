"""WebSocket server: fans encoded frames out to clients, receives acks.

Platform-agnostic. Auth and discovery are added here in milestone 6.

WIRE FORMAT (server -> client, binary message):
    offset 0  uint8   message type, 1 = JPEG frame
    offset 1  uint32  frame id (little-endian)
    offset 5  uint16  JPEG width
    offset 7  uint16  JPEG height
    offset 9  ...     JPEG bytes
Everything else in both directions is a JSON text message with a "type" field.

SERVER -> CLIENT JSON messages: hello, stats, auth_failed, and
    cursor  {"x": f, "y": f}      PC pointer position as a fraction of the
                                  streamed monitor (outside 0..1 = off-screen).
                                  Needed because screen capture omits the pointer.

CLIENT -> SERVER JSON messages:
    auth    {"pin": "123456"}     must be the FIRST message from any non-local
                                  client; wrong/missing PIN closes with code 4401
    ack     {"id": <frame id>}
    move    {"dx": f, "dy": f}    relative cursor move, as a FRACTION of the
                                  monitor size (0.01 = 1% of the screen width)
    abs     {"x": f, "y": f}      absolute cursor position, 0..1 within the monitor
    button  {"btn": "left"|"right"|"middle", "down": bool}
    scroll  {"dx": f, "dy": f}    wheel notches; dy > 0 scrolls the content down
    key     {"name": str, "down": bool}
    text    {"text": str}         typed as literal Unicode text

WHY binary for frames: base64 inside JSON would add 33% to every frame and
force an extra encode/decode on both ends.

WHY positions are fractions of the monitor, not pixels: the phone only knows
the (downscaled) stream size. Fractions stay correct however the stream is
scaled and whatever the PC's real resolution/DPI is; the server owns the one
conversion to desktop pixels.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import math
import struct
import time
from typing import Optional

from aiohttp import WSMsgType, web

from .capture import CaptureThread, EncodedFrame
from .config import AccessConfig, StreamConfig
from .platform.base import InputSink, NullInputSink
from .testpage import TEST_PAGE_HTML

AUTH_TIMEOUT_S = 5.0
CLOSE_AUTH_FAILED = 4401  # app-defined WebSocket close code (4000-4999 is free to use)
MAX_PIN_FAILURES = 5      # per client address, then locked out for LOCKOUT_S
LOCKOUT_S = 60.0
MAX_TEXT_CHARS = 256

log = logging.getLogger(__name__)

# "<" = little-endian with no padding. Little-endian because both x86 PCs and
# ARM phones are little-endian, and Dart's ByteData takes an explicit Endian.
FRAME_HEADER = struct.Struct("<BIHH")
MSG_FRAME = 1


class Client:
    """Per-connection state."""

    def __init__(self, ws: web.WebSocketResponse, peer: str) -> None:
        self.ws = ws
        self.peer = peer
        # Set whenever a newer frame is available for this client.
        self.new_frame = asyncio.Event()
        # frame id -> time it was handed to the socket. Used to turn an ack
        # into a latency sample.
        self.sent_at: dict[int, float] = {}
        # Counters for the current stats window (reset every second).
        self.offered = 0    # frames produced while this client was connected
        self.sent = 0       # frames actually written to the socket
        self.latency_ms: list[float] = []
        # Mouse buttons this client is currently holding down. If the phone
        # vanishes mid-drag the button would stay pressed on the PC forever,
        # so the disconnect handler releases whatever is in here.
        self.pressed: set[str] = set()


class StreamHub:
    """Holds the single newest frame and the set of connected clients.

    STALE-FRAME DROPPING lives here, and it is structural rather than a rule
    that has to be enforced: there is no queue. There is one slot, `latest`,
    which the capture thread keeps overwriting. Each client's sender takes
    whatever is in the slot when the socket is ready for more. A slow client
    therefore skips frames automatically and always gets the most recent
    picture, which is what matters for remote control (an old frame shows a
    mouse position that is no longer true).
    """

    def __init__(
        self,
        capture: CaptureThread,
        config: StreamConfig,
        sink: InputSink,
        access: AccessConfig,
    ) -> None:
        self.capture = capture
        self.config = config
        self.sink = sink
        self.access = access
        self.clients: set[Client] = set()
        self.latest: Optional[EncodedFrame] = None
        # client address -> (failed attempts, time of the last failure)
        self.pin_failures: dict[str, tuple[int, float]] = {}

    def publish(self, frame: EncodedFrame) -> None:
        """Runs on the event loop thread (scheduled via call_soon_threadsafe)."""
        self.latest = frame
        for c in self.clients:
            c.offered += 1
            c.new_frame.set()

    def add(self, client: Client) -> None:
        self.clients.add(client)
        self.capture.set_active(True)

    def remove(self, client: Client) -> None:
        self.clients.discard(client)
        if not self.clients:
            # Nobody watching: park the capture thread and forget the last
            # frame so the next client never starts with an old picture.
            self.capture.set_active(False)
            self.latest = None


async def _send_frames(hub: StreamHub, client: Client) -> None:
    """Per-client sender: forward the newest frame whenever the socket is free."""
    ws = client.ws
    while True:
        await client.new_frame.wait()
        client.new_frame.clear()
        frame = hub.latest
        if frame is None:
            continue

        header = FRAME_HEADER.pack(MSG_FRAME, frame.frame_id, frame.width, frame.height)
        client.sent_at[frame.frame_id] = time.perf_counter()
        # send_bytes only returns once the data is in the OS send buffer. If
        # the link is slower than the frame rate this await takes a while;
        # meanwhile hub.latest keeps being replaced, and the next loop
        # iteration picks up only the newest frame. That is the back-pressure.
        await ws.send_bytes(header + frame.jpeg)
        client.sent += 1

        # An ack may never arrive (frame skipped by the client, client hung).
        # Cap the table so it cannot grow without bound.
        if len(client.sent_at) > 300:
            for fid in sorted(client.sent_at)[:-150]:
                del client.sent_at[fid]


def _num(msg: dict, key: str, limit: float) -> Optional[float]:
    """A finite number from `msg`, clamped to +-limit; None if absent/invalid.

    WHY so strict: these values come off the network and end up as arguments
    to OS input calls. NaN/inf/huge values or non-numbers must be dropped here.
    bool is excluded explicitly because it is a subclass of int in Python.
    """
    v = msg.get(key)
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        return None
    return max(-limit, min(limit, float(v)))


def _handle_input(hub: StreamHub, client: Client, kind: str, msg: dict) -> None:
    mon = hub.capture.monitor
    sink = hub.sink
    if mon is None:
        return

    if kind == "move":
        dx, dy = _num(msg, "dx", 1.0), _num(msg, "dy", 1.0)
        if dx is not None and dy is not None:
            sink.mouse_move_rel(round(dx * mon.width), round(dy * mon.height))

    elif kind == "abs":
        x, y = _num(msg, "x", 1e6), _num(msg, "y", 1e6)
        if x is not None and y is not None:
            x, y = min(1.0, max(0.0, x)), min(1.0, max(0.0, y))
            sink.mouse_move_to(mon.left + round(x * (mon.width - 1)), mon.top + round(y * (mon.height - 1)))

    elif kind == "button":
        btn, down = msg.get("btn"), msg.get("down")
        if btn in ("left", "right", "middle") and isinstance(down, bool):
            if down:
                client.pressed.add(btn)
            else:
                client.pressed.discard(btn)
            sink.mouse_button(btn, down)

    elif kind == "scroll":
        dx, dy = _num(msg, "dx", 50.0) or 0.0, _num(msg, "dy", 50.0) or 0.0
        if dx or dy:
            sink.scroll(dx, dy)

    elif kind == "key":
        name, down = msg.get("name"), msg.get("down")
        if isinstance(name, str) and isinstance(down, bool):
            sink.key(name, down)

    elif kind == "text":
        text = msg.get("text")
        if isinstance(text, str) and text:
            sink.type_text(text[:MAX_TEXT_CHARS])


def _release_buttons(hub: StreamHub, client: Client) -> None:
    for btn in list(client.pressed):
        try:
            hub.sink.mouse_button(btn, False)
        except Exception:
            log.exception("Could not release %s button", btn)
    client.pressed.clear()


def _handle_message(hub: StreamHub, client: Client, text: str) -> None:
    try:
        msg = json.loads(text)
        kind = msg.get("type")
    except (ValueError, AttributeError):
        # Malformed input must never crash the connection handler: this port
        # is reachable by anything on the LAN.
        return

    if kind == "ack":
        # The client finished DECODING this frame. now - send time is therefore
        # network there + decode + network back: a slight overestimate of
        # "time until the user sees it", which is the number we care about.
        frame_id = msg.get("id")
        sent = client.sent_at.pop(frame_id, None) if isinstance(frame_id, int) else None
        if sent is not None:
            client.latency_ms.append((time.perf_counter() - sent) * 1000)
            # Anything older than an acked frame was skipped by the client and
            # will never be acked.
            for fid in [f for f in client.sent_at if f < frame_id]:
                del client.sent_at[fid]
    elif kind in ("move", "abs", "button", "scroll", "key", "text"):
        # View-only mode: the stream works, the phone just cannot touch the PC.
        if hub.access.allow_input:
            try:
                _handle_input(hub, client, kind, msg)
            except Exception:
                # A bad message or an OS call that fails must not drop the
                # connection (and with it the picture).
                log.exception("Input message %r failed", kind)


async def _send_cursor(hub: StreamHub, client: Client) -> None:
    """Per-client: tell the phone where the PC's mouse pointer is.

    A separate task per client (not one shared ticker) so a phone whose socket
    is backed up only delays its own pointer updates. Sends nothing while the
    pointer is still, so an idle desktop costs no bandwidth.
    """
    last = None
    while True:
        await asyncio.sleep(1 / 30)
        mon = hub.capture.monitor
        pos = hub.sink.cursor_pos()
        if mon is None or pos is None or pos == last:
            continue
        last = pos
        # Fractions of the streamed monitor; outside 0..1 means the pointer is
        # on another monitor and the app hides it.
        fx = round((pos[0] - mon.left) / mon.width, 4)
        fy = round((pos[1] - mon.top) / mon.height, 4)
        await client.ws.send_str(json.dumps({"type": "cursor", "x": fx, "y": fy}))


def _is_local(peer: str) -> bool:
    return peer in ("127.0.0.1", "::1")


async def _authenticate(hub: StreamHub, client: Client) -> bool:
    """Check the PIN; on failure close the socket and return False.

    Connections from this PC itself skip the check: anything running locally
    can already control the PC, so a PIN would add friction and no security
    (and the browser test page keeps working without it).
    """
    pin = hub.access.pin
    if not pin or _is_local(client.peer):
        return True

    fails, last = hub.pin_failures.get(client.peer, (0, 0.0))
    if fails >= MAX_PIN_FAILURES:
        if time.monotonic() - last < LOCKOUT_S:
            # Don't even look at the attempt: a locked-out guesser learns
            # nothing, and cannot use us to test PINs faster than 5 per minute.
            await client.ws.close(code=CLOSE_AUTH_FAILED, message=b"locked out")
            return False
        fails = 0

    ok = False
    try:
        msg = await client.ws.receive(timeout=AUTH_TIMEOUT_S)
        if msg.type == WSMsgType.TEXT:
            data = json.loads(msg.data)
            supplied = data.get("pin") if data.get("type") == "auth" else None
            # compare_digest: constant-time, so response timing cannot be used
            # to guess the PIN one digit at a time.
            ok = isinstance(supplied, str) and hmac.compare_digest(supplied.encode(), pin.encode())
    except (asyncio.TimeoutError, ValueError, AttributeError):
        ok = False

    if ok:
        hub.pin_failures.pop(client.peer, None)
        return True

    hub.pin_failures[client.peer] = (fails + 1, time.monotonic())
    log.warning("Rejected %s: wrong or missing PIN (%d recent failures)", client.peer, fails + 1)
    try:
        await client.ws.send_json({"type": "auth_failed"})
        await client.ws.close(code=CLOSE_AUTH_FAILED, message=b"bad pin")
    except ConnectionError:
        pass
    return False


async def _ws_handler(request: web.Request) -> web.WebSocketResponse:
    hub: StreamHub = request.app["hub"]

    ws = web.WebSocketResponse(
        # JPEG is already compressed; deflating it again costs CPU for ~0% gain.
        # It also keeps every send a single uninterrupted write, which is what
        # makes it safe for the frame sender and the stats ticker to share
        # this socket without a lock.
        compress=False,
        # Ping every 10 s; drop the connection if no pong. Without this a
        # phone that walks out of Wi-Fi range stays "connected" for minutes
        # and keeps the capture thread running.
        heartbeat=10.0,
        # Clients only ever send small JSON. A tight limit stops a misbehaving
        # peer from making the server buffer megabytes.
        max_msg_size=64 * 1024,
    )
    await ws.prepare(request)

    client = Client(ws, request.remote or "?")
    if not await _authenticate(hub, client):
        return ws
    log.info("Client connected: %s", client.peer)

    cap = hub.capture
    await ws.send_json(
        {
            "type": "hello",
            # Lets the app grey out its mouse buttons in view-only mode.
            "input": hub.access.allow_input,
            "monitors": [
                {"index": m.index, "width": m.width, "height": m.height, "primary": m.is_primary}
                for m in cap.monitors
            ],
            "monitor": cap.monitor.index,
            # Real screen size: the client needs it later to map touch
            # positions onto the desktop (direct mode).
            "screen": {"width": cap.monitor.width, "height": cap.monitor.height},
            "stream": {
                "max_width": hub.config.max_width,
                "quality": hub.config.jpeg_quality,
                "fps": hub.config.fps,
            },
        }
    )

    hub.add(client)
    sender = asyncio.create_task(_send_frames(hub, client))
    cursor_task = asyncio.create_task(_send_cursor(hub, client))
    try:
        # This loop ends when the client disconnects or the heartbeat fails.
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                _handle_message(hub, client, msg.data)
    finally:
        _release_buttons(hub, client)
        hub.remove(client)
        sender.cancel()
        cursor_task.cancel()
        # Await the cancelled tasks so their exceptions (if any) are consumed
        # instead of being logged as "Task exception was never retrieved".
        await asyncio.gather(sender, cursor_task, return_exceptions=True)
        log.info("Client disconnected: %s", client.peer)
    return ws


async def _stats_ticker(hub: StreamHub) -> None:
    """Once a second: push measurements to every client and to the log.

    These numbers are the raw material for the adaptive controller in
    milestone 3; for now they are just displayed so the ladder can be tuned.
    """
    tick = 0
    while True:
        await asyncio.sleep(1.0)
        cs = hub.capture.take_stats()
        if not hub.clients:
            continue
        tick += 1
        n = max(1, cs.frames)
        capture_part = {
            "capture_fps": cs.frames,
            "reused": cs.reused,
            "grab_ms": round(cs.grab_ms / n, 1),
            "encode_ms": round(cs.encode_ms / n, 1),
            "frame_kb": round(cs.jpeg_bytes / n / 1024, 1),
        }
        for c in list(hub.clients):
            lat = c.latency_ms
            stats = {
                "type": "stats",
                **capture_part,
                "sent_fps": c.sent,
                # Frames produced but never sent to this client because the
                # socket was still busy with an earlier one.
                "dropped": max(0, c.offered - c.sent),
                "kbps": round(c.sent * cs.jpeg_bytes / n * 8 / 1000),
                "latency_ms": round(sum(lat) / len(lat), 1) if lat else None,
                "latency_max_ms": round(max(lat), 1) if lat else None,
                # Sent but not yet acked = frames in flight. Will be the main
                # "is the link struggling" signal for the adaptive controller.
                "in_flight": len(c.sent_at),
            }
            c.offered = c.sent = 0
            c.latency_ms = []
            if tick % 5 == 0:  # console every 5 s; every second is just noise
                log.info("%s %s", c.peer, {k: v for k, v in stats.items() if k != "type"})
            try:
                await c.ws.send_json(stats)
            except (ConnectionError, RuntimeError):
                pass  # client is going away; its handler does the cleanup


async def _index(_request: web.Request) -> web.Response:
    return web.Response(text=TEST_PAGE_HTML, content_type="text/html")


def build_app(
    config: StreamConfig,
    source,
    sink: Optional[InputSink] = None,
    access: Optional[AccessConfig] = None,
) -> web.Application:
    """Wire capture -> hub -> WebSocket clients into an aiohttp application."""
    app = web.Application()
    sink = sink or NullInputSink()
    access = access or AccessConfig()

    async def lifecycle(app: web.Application):
        # --- startup ---
        loop = asyncio.get_running_loop()

        def on_frame(frame: EncodedFrame) -> None:
            # Called on the capture thread. asyncio objects are not
            # thread-safe, so hop over to the event loop thread.
            try:
                loop.call_soon_threadsafe(hub.publish, frame)
            except RuntimeError:
                pass  # loop already closed during shutdown

        capture = CaptureThread(source, config, on_frame)
        hub = StreamHub(capture, config, sink, access)
        app["hub"] = hub
        capture.start()
        # Wait (without blocking the loop) until the monitor list is known.
        await loop.run_in_executor(None, capture.ready.wait)
        if capture.error is not None:
            raise RuntimeError(f"Screen capture failed to start: {capture.error}") from capture.error
        m = capture.monitor
        log.info("Capturing monitor %d (%dx%d)", m.index, m.width, m.height)

        ticker = asyncio.create_task(_stats_ticker(hub))
        yield
        # --- shutdown ---
        ticker.cancel()
        await asyncio.gather(ticker, return_exceptions=True)
        for c in list(hub.clients):
            await c.ws.close()
        capture.stop()

    app.cleanup_ctx.append(lifecycle)
    app.router.add_get("/", _index)
    app.router.add_get("/ws", _ws_handler)
    return app
