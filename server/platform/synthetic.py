"""Synthetic ScreenSource: a fake 1920x1080 "screen" with a moving bar.

WHY this exists: it lets the whole pipeline (encode -> WebSocket -> browser)
run on a machine with no capturable display (CI, a headless box, or a PC where
the real backend is misbehaving and you want to know whether the fault is in
capture or in everything after it). Select it with --backend test.

The on-screen millisecond clock doubles as a crude end-to-end latency probe:
photograph the PC console clock and the phone side by side if needed.
"""

from __future__ import annotations

import time
from typing import Optional

from PIL import Image, ImageDraw

from .base import Monitor, RawFrame, ScreenSource

_W, _H = 1920, 1080


class SyntheticScreenSource(ScreenSource):
    def __init__(self) -> None:
        # Pre-render the static background once. Drawing a gradient per frame
        # would dominate the timing numbers we are trying to measure.
        self._background = Image.linear_gradient("L").resize((_W, _H)).convert("RGB")
        self._count = 0

    def list_monitors(self) -> list[Monitor]:
        return [Monitor(index=0, left=0, top=0, width=_W, height=_H, is_primary=True)]

    def grab(self, monitor: Monitor) -> Optional[RawFrame]:
        self._count += 1
        img = self._background.copy()
        draw = ImageDraw.Draw(img)
        # A bar that crosses the screen every 4 seconds: makes dropped frames
        # and stutter visible to the eye.
        x = int((time.time() % 4.0) / 4.0 * (_W - 200))
        draw.rectangle([x, 400, x + 200, 680], fill=(220, 60, 60))
        draw.text(
            (40, 40),
            f"PhoneDesk test source  frame {self._count}  t={time.time() % 1000:8.3f}s",
            fill=(255, 255, 0),
            font_size=48,
        )
        return RawFrame(_W, _H, img.tobytes(), "RGB")
