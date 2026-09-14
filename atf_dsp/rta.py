"""rta.py — Input Signal Analyzer / RTA reader (client-polled).

The device does not push a stream; the PC-Tool selects a channel (a channel-select
mux cell) and repeatedly READS peak/RMS readback cells via 0x02, converting each
8.24 fixed-point cell to a level. This module mirrors that: pick a channel, poll the
readback cells, convert to dBFS.

Cells and mux/reset addresses come from protocol.yaml.rta (status: partial — the
exact polling set/cadence still needs a PC-Tool capture, but the read path is the
confirmed 0x02 primitive and is side-effect-free).
"""
from __future__ import annotations

import math
from typing import Dict, Optional

from atf_dsp.contract import rta_spec
from atf_dsp.device import Device
from atf_dsp.encoding import from_fixed

_SPEC = rta_spec()
READBACK_CELLS: Dict[str, int] = {k: int(v) for k, v in _SPEC.get("readback_cells", {}).items()}
CHANNEL_SELECT_ADDR: int = int(_SPEC["channel_select"])
PEAK_RESET_ADDR: int = int(_SPEC["peak_reset"])

# Level floor for a silent cell (0.0 -> -inf); expose a finite floor for convenience.
DBFS_FLOOR = -144.0


def linear_to_dbfs(value: float, floor: float = DBFS_FLOOR) -> float:
    """Convert a linear magnitude (1.0 == full scale) to dBFS, floored for silence."""
    magnitude = abs(value)
    if magnitude <= 0:
        return floor
    return max(floor, 20.0 * math.log10(magnitude))


class RTA:
    """Poll the ISA/RTA readback cells of a connected (or dry-run) Device."""

    cells = READBACK_CELLS

    def __init__(self, device: Device) -> None:
        self.device = device

    # -- channel mux / peak-hold ------------------------------------------
    def select_channel(self, channel: int) -> bytes:
        """Write the channel index to the RTA channel-select mux cell."""
        data = (channel & 0xFFFFFFFF).to_bytes(4, "big")
        return self.device.write_param(CHANNEL_SELECT_ADDR, data)

    def reset_peak(self) -> bytes:
        """Reset the peak-hold by writing the reset cell."""
        return self.device.write_param(PEAK_RESET_ADDR, b"\x00\x00\x00\x01")

    # -- reads (side-effect-free) -----------------------------------------
    def read_raw(self, cell: str) -> Optional[float]:
        """Read one named readback cell and decode it as an 8.24 linear value."""
        if cell not in READBACK_CELLS:
            raise KeyError(f"unknown RTA cell {cell!r}; known: {sorted(READBACK_CELLS)}")
        data = self.device.read_param(READBACK_CELLS[cell], nbytes=4)
        if len(data) < 4:
            return None
        return from_fixed(int.from_bytes(data[:4], "big"))

    def read_dbfs(self, cell: str, floor: float = DBFS_FLOOR) -> Optional[float]:
        """Read one named readback cell as a dBFS level."""
        value = self.read_raw(cell)
        if value is None:
            return None
        return linear_to_dbfs(value, floor=floor)

    def levels(self, channel: Optional[int] = None, floor: float = DBFS_FLOOR) -> Dict[str, float]:
        """Optionally select *channel*, then read every readback cell as dBFS."""
        if channel is not None:
            self.select_channel(channel)
        out: Dict[str, float] = {}
        for name in READBACK_CELLS:
            level = self.read_dbfs(name, floor=floor)
            if level is not None:
                out[name] = level
        return out
