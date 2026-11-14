"""Central place for tunable defaults.

WHY a dataclass instead of module-level constants: the adaptive controller
(milestone 3) will change width/quality/fps at runtime, and token storage
(milestone 6) will add persisted fields. Passing one object around keeps that
change local instead of hunting globals across modules.
"""

from __future__ import annotations

from dataclasses import dataclass

# Fixed default port (open question in the plan: fixed vs random). Fixed makes
# firewall rules and manual "type the address" connections simple. Overridable
# with --port.
DEFAULT_PORT = 8765


@dataclass
class StreamConfig:
    # Milestone 1 uses step 0 of the planned quality ladder: 640 wide, q40.
    max_width: int = 640     # frames are downscaled to this width (never upscaled)
    jpeg_quality: int = 40   # Pillow JPEG quality, 1..95
    fps: int = 15            # capture/encode target
    monitor: int = -1        # index into list_monitors(); -1 = primary
