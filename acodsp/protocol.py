"""protocol.py — ACO opcode constants and low-level typed commands.

These are thin encoders over the framing in transport.py. The pure `encode_*`
functions build payloads (unit-testable without hardware); the `Protocol` class
binds them to a `Link` and returns response payloads.

Confirmed opcodes (payload[0]), see protocol.yaml:
    0x02 dsp_read   : 02 <instance> <addr_hi> <addr_lo> [N pad]        (addr 16-bit big-endian)
    0x03 dsp_write  : 03 <instance> <flag> <addr_hi> <addr_lo> <data>  (data 32-bit big-endian)
    0x08 identify   : 08 01
    0x1F config_set : 1F <a_hi> <a_lo> <sel> <p4> <p5> <p6> <p7>       (sel=9 selects setup)
    0x28 get_group  : 28 <sub> [args]
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from acodsp.transport import Link

# -- opcodes ---------------------------------------------------------------
OP_DSP_READ = 0x02
OP_DSP_WRITE = 0x03
OP_IDENTIFY = 0x08
OP_CONFIG_SET = 0x1F
OP_CONFIG_GET = 0x1E
OP_GET_GROUP = 0x28
OP_SET_GROUP_REMOTE = 0x29  # DANGER: remote group mutes — never use for control.

# -- 0x1F config sub-ops ---------------------------------------------------
CFG_SELECT_SETUP = 0x09

# -- 0x28 GET sub-ops ------------------------------------------------------
GET_MANUAL_ENABLE = 0x01
GET_SOURCE_CFG = 0x20
GET_CURRENT_SETUP = 0x40
GET_TELEMETRY = 0x50
GET_SLOT_ENABLED = 0x51

# -- 0x03 write flag -------------------------------------------------------
WRITE_FLAG_DIRECT = 0xFF     # forces the direct-write path
WRITE_FLAG_SAFELOAD = 0x00   # any value != 0xFF selects SafeLoad (when payload < 0x1a)

# instance gate is (instance & 7) == 0 — use 0.
DEFAULT_INSTANCE = 0
DEFAULT_WORD_SIZE = 4        # DSP words are 32-bit
SAFELOAD_MAX_PAYLOAD = 0x1A  # payload length must be < 0x1a for the SafeLoad path


def _addr_bytes(addr: int) -> bytes:
    """16-bit DSP address, big-endian (MSB first)."""
    if not 0 <= addr <= 0xFFFF:
        raise ValueError(f"address out of range: {addr}")
    return bytes([(addr >> 8) & 0xFF, addr & 0xFF])


# -- pure payload encoders -------------------------------------------------
def encode_get(sub: int, args: bytes = b"") -> bytes:
    """0x28 GET: `28 <sub> [args]`."""
    return bytes([OP_GET_GROUP, sub & 0xFF]) + bytes(args)


def encode_identify() -> bytes:
    """0x08 identify: `08 01`."""
    return bytes([OP_IDENTIFY, 0x01])


def encode_select_setup(idx: int) -> bytes:
    """0x1F config set, sel=9: `1F 00 00 09 00 <idx> 00 00` (idx is 0-based)."""
    if not 0 <= idx <= 0xFF:
        raise ValueError(f"setup index out of range: {idx}")
    return bytes([OP_CONFIG_SET, 0x00, 0x00, CFG_SELECT_SETUP, 0x00, idx, 0x00, 0x00])


def encode_read_param(instance: int, addr: int, nbytes: int = DEFAULT_WORD_SIZE) -> bytes:
    """0x02 DSP read: `02 <inst> <addr_hi> <addr_lo>` + nbytes trailing pad.

    The number of trailing pad bytes sets the read size (N = payload_len - 4).
    """
    if nbytes < 0:
        raise ValueError("nbytes must be >= 0")
    return bytes([OP_DSP_READ, instance & 0xFF]) + _addr_bytes(addr) + bytes(nbytes)


def encode_write_param(instance: int, addr: int, data: bytes, safeload: bool = True) -> bytes:
    """0x03 DSP write: `03 <inst> <flag> <addr_hi> <addr_lo> <data BE...>`.

    flag=0xFF forces a direct write; otherwise the firmware uses SafeLoad when the
    total payload length is < 0x1a. `data` must already be big-endian.
    """
    flag = WRITE_FLAG_SAFELOAD if safeload else WRITE_FLAG_DIRECT
    return bytes([OP_DSP_WRITE, instance & 0xFF, flag]) + _addr_bytes(addr) + bytes(data)


class Protocol:
    """Bind the pure encoders to a Link and return response payloads."""

    def __init__(self, link: "Link") -> None:
        self.link = link

    def get(self, sub: int, args: bytes = b"", read_timeout: float = 0.3) -> bytes:
        return self.link.command(encode_get(sub, args), read_timeout)

    def identify(self, read_timeout: float = 0.5) -> bytes:
        return self.link.command(encode_identify(), read_timeout)

    def select_setup(self, idx: int, read_timeout: float = 2.0) -> bytes:
        """Switch to a stored setup by 0-based index via the clean 0x1F path (no mute)."""
        return self.link.command(encode_select_setup(idx), read_timeout)

    def read_param(
        self,
        addr: int,
        nbytes: int = DEFAULT_WORD_SIZE,
        instance: int = DEFAULT_INSTANCE,
        read_timeout: float = 0.3,
    ) -> bytes:
        return self.link.command(encode_read_param(instance, addr, nbytes), read_timeout)

    def write_param(
        self,
        addr: int,
        data: bytes,
        safeload: bool = True,
        instance: int = DEFAULT_INSTANCE,
        read_timeout: float = 0.3,
    ) -> bytes:
        return self.link.command(
            encode_write_param(instance, addr, data, safeload), read_timeout
        )
