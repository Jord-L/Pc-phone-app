"""Persisted user settings (what the GUI edits).

WHY a JSON file in %APPDATA% and not next to the exe: the exe may live in a
read-only place (Program Files, a USB stick) and a PyInstaller one-file exe
unpacks to a temp folder that is deleted on exit. APPDATA is writable, per-user,
and survives rebuilding the exe.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import sys
from dataclasses import asdict, dataclass, fields
from pathlib import Path

from .config import DEFAULT_PORT, StreamConfig

log = logging.getLogger(__name__)


def new_pin() -> str:
    # `secrets`, not `random`: the PIN is the only thing between the LAN and
    # control of this PC, so it must not be predictable.
    return f"{secrets.randbelow(1_000_000):06d}"


@dataclass
class Settings:
    port: int = DEFAULT_PORT
    # True = accept phones on the network (needed for the app to work at all);
    # False = this PC only (browser test page).
    lan: bool = True
    allow_input: bool = True
    max_width: int = StreamConfig.max_width
    jpeg_quality: int = StreamConfig.jpeg_quality
    fps: int = StreamConfig.fps
    monitor: int = -1
    pin: str = ""
    # Start sharing as soon as the window opens, so launching the exe is enough.
    autostart_service: bool = True
    backend: str = "auto"

    def stream_config(self) -> StreamConfig:
        return StreamConfig(
            max_width=min(3840, max(160, self.max_width)),
            jpeg_quality=min(95, max(1, self.jpeg_quality)),
            fps=min(60, max(1, self.fps)),
            monitor=self.monitor,
        )


def settings_path() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home() / ".config")
    return Path(base) / "PhoneDesk" / "settings.json"


def load() -> Settings:
    s = Settings()
    try:
        raw = json.loads(settings_path().read_text(encoding="utf-8"))
        known = {f.name: f.type for f in fields(Settings)}
        for key, value in raw.items():
            # Ignore unknown keys (older/newer version) and wrong types (a
            # hand-edited file) instead of crashing at startup.
            if key in known and type(value) is type(getattr(s, key)):
                setattr(s, key, value)
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as exc:
        log.warning("Could not read settings (%s); using defaults", exc)
    if not (s.pin.isdigit() and len(s.pin) == 6):
        s.pin = new_pin()
        save(s)
    return s


def save(s: Settings) -> None:
    try:
        p = settings_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        # Write-then-rename so a crash mid-write cannot leave a half file that
        # would reset the PIN on the next start.
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(s), indent=2), encoding="utf-8")
        os.replace(tmp, p)
    except OSError as exc:
        log.warning("Could not save settings: %s", exc)


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))
