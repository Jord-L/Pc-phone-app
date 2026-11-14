"""WebSocket server: fans encoded frames out to clients, receives acks.

Platform-agnostic. Auth and discovery are added here in milestone 6.

WIRE FORMAT (server -> client, binary message):
    offset 0  uint8   message type, 1 = JPEG frame
    offset 1  uint32  frame id (little-endian)
    offset 5  uint16  JPEG width
    offset 7  uint16  JPEG height
    offset 9  ...     JPEG bytes
Everything else in both directions is a JSON text message with a "type" field.

WHY binary for frames: base64 inside JSON would add 33% to every frame and
force an extra encode/decode on both ends.
"""

from __future__ import annotations

import asyncio
import json
import logging
import struct
import time
from typing import Optional

from aiohttp import WSMsgType, web

from .capture import CaptureThread, EncodedFrame
from .config import StreamConfig
from .testpage import TEST_PAGE_HTML

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

    def __init__(self, capture: CaptureThread, config: StreamConfig) -> None:
        self.capture = capture
        self.config = config
        self.clients: set[Client] = set()
        self.latest: Optional[EncodedFrame] = None

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


def _handle_message(client: Client, text: str) -> None:
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
    # Input messages (move, click, key, ...) arrive here from milestone 4 on.


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
    log.info("Client connected: %s", client.peer)

    cap = hub.capture
    await ws.send_json(
        {
            "type": "hello",
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
    try:
        # This loop ends when the client disconnects or the heartbeat fails.
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                _handle_message(client, msg.data)
    finally:
        hub.remove(client)
        sender.cancel()
        # Await the cancelled task so its exception (if any) is consumed
        # instead of being logged as "Task exception was never retrieved".
        await asyncio.gather(sender, return_exceptions=True)
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


def build_app(config: StreamConfig, source) -> web.Application:
    """Wire capture -> hub -> WebSocket clients into an aiohttp application."""
    app = web.Application()

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
        hub = StreamHub(capture, config)
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
