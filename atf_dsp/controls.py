"""controls.py — typed tuning API over a Device (gain / mute / delay / EQ).

Each method resolves a param name (or raw address) via the Device's ParamMap,
encodes the value with the pure functions in encoding.py, and writes it glitch-free
through the firmware's own SafeLoad path (``Device.write_param(safeload=True)`` —
a 0x03 write with flag 0x00; the firmware builds the SafeLoad block, never the
host). Encoders whose protocol.yaml status is not
``confirmed`` (delay, crossover, routing) are gated: they raise NotConfirmedError
unless the caller passes ``unsafe=True``, so nothing unvalidated writes silently.
"""
from __future__ import annotations

from typing import Dict, List, Optional, Union

from atf_dsp import encoding
from atf_dsp.contract import load_contract
from atf_dsp.device import AddrOrName, Device
from atf_dsp.encoding import EQ_STORAGE_ORDER, UNITY

MUTE_RAW = 0x00000000
UNMUTE_RAW = UNITY  # 0x01000000


class NotConfirmedError(RuntimeError):
    """Raised when a typed control whose contract status is not `confirmed` is used
    without an explicit unsafe=True opt-in."""


def _encoder_status(type_name: str) -> str:
    """Return the protocol.yaml.encoders.<type>.status (or 'unknown')."""
    enc = load_contract().get("encoders", {}).get(type_name, {})
    return str(enc.get("status", "unknown"))


class Controls:
    """Typed control surface bound to a connected (or dry-run) Device."""

    def __init__(self, device: Device) -> None:
        self.device = device

    # -- helpers ----------------------------------------------------------
    def _addr(self, target: AddrOrName) -> int:
        return self.device._resolve_addr(target)

    def _require_confirmed(self, type_name: str, unsafe: bool) -> None:
        status = _encoder_status(type_name)
        if status != "confirmed" and not unsafe:
            raise NotConfirmedError(
                f"encoder '{type_name}' is status='{status}' in protocol.yaml, not hardware-confirmed. "
                f"Pass unsafe=True to write it anyway (it may not behave as expected)."
            )

    def _write_words(self, addr: int, raws: List[int]) -> bytes:
        """Write 1..5 consecutive 32-bit words starting at *addr* via the ACO firmware's
        SafeLoad path: one 0x03 frame (safeload selector) — the firmware builds the SafeLoad
        block and applies it to its runtime window base. (Do NOT build the block host-side;
        the window base is compiler-assigned per unit, read at runtime from RAM 0x20000c22 —
        hardware-confirmed 2026-08-26 that the host-side base 0x0014 does not apply.)"""
        data = b"".join(encoding.to_bytes_be(r) for r in raws)
        return self.device.write_param(addr, data, safeload=True)

    def _write_word(self, addr: int, raw: int) -> bytes:
        """Write one 32-bit word to *addr* (firmware SafeLoad)."""
        return self._write_words(addr, [raw])

    # -- scalar gain (confirmed) ------------------------------------------
    def set_gain(self, target: AddrOrName, db: float) -> bytes:
        """Set a gain/volume param (dB -> 8.24 linear multiplier) via SafeLoad."""
        raw = encoding.gain_db_to_raw(db)
        return self._write_word(self._addr(target), raw)

    # -- mute (confirmed) -------------------------------------------------
    def set_mute(self, target: AddrOrName, muted: bool) -> bytes:
        """Mute (0x00000000) or unmute (0x01000000) a dedicated MUTE gain cell."""
        raw = MUTE_RAW if muted else UNMUTE_RAW
        return self._write_word(self._addr(target), raw)

    # -- delay (hypothesis -> gated) --------------------------------------
    def set_delay(self, target: AddrOrName, ms: float, fs: int = encoding.DEFAULT_FS,
                  unsafe: bool = False) -> bytes:
        """Set a time-alignment delay (ms -> integer sample count, 32.0 format).

        Gated: protocol.yaml.encoders.delay is status 'hypothesis' (internal fs and
        cell semantics need hardware); pass unsafe=True to write anyway.
        """
        self._require_confirmed("delay", unsafe)
        raw = encoding.delay_ms_to_samples(ms, fs=fs)
        return self._write_word(self._addr(target), raw)

    # -- EQ band (confirmed) ----------------------------------------------
    def set_eq_band(self, base: AddrOrName, f0: float, q: float, gain_db: float,
                    kind: str = "peaking", fs: int = encoding.DEFAULT_FS) -> bytes:
        """Write one EQ band's 5 biquad coefficients [B2,B1,B0,A2,A1] atomically.

        *base* is the address (or name) of the first coefficient (STAGE..._B2); the
        remaining four live at consecutive addresses. All five are written in a single
        SafeLoad burst so the change is glitch-free.
        """
        base_addr = self._addr(base)
        coeffs = encoding.biquad_rbj(kind, f0, q, gain_db, fs=fs)
        return self._write_words(base_addr, coeffs)

    # -- crossover / routing (hypothesis -> gated) ------------------------
    def set_crossover(self, base: AddrOrName, f0: float, q: float = 0.7071,
                      kind: str = "lowpass", fs: int = encoding.DEFAULT_FS,
                      unsafe: bool = False) -> bytes:
        """Write a single crossover biquad stage. Gated: encoders.crossover is
        status 'hypothesis' (per-slope stage mapping needs a capture)."""
        self._require_confirmed("crossover", unsafe)
        base_addr = self._addr(base)
        coeffs = encoding.biquad_rbj(kind, f0, q, 0.0, fs=fs)
        return self._write_words(base_addr, coeffs)

    def bypass_crossover(self, base: AddrOrName, unsafe: bool = False) -> bytes:
        """Stamp the pass-through bypass image ``[0, 0, UNITY, 0, 0]`` onto a crossover stage so an
        unused section / stage is explicitly OFF (H(z)=1), making a crossover write idempotent
        regardless of prior DSP state. Same gate as :meth:`set_crossover`; recovery reads this image
        back as ``None`` (no active stage)."""
        self._require_confirmed("crossover", unsafe)
        return self._write_words(self._addr(base), [0, 0, encoding.UNITY, 0, 0])

    def set_routing(self, target: AddrOrName, gain_db: float = 0.0,
                    unsafe: bool = False) -> bytes:
        """Set a MainMatrix mix-cell gain (8.24). Gated: encoders.routing is status
        'hypothesis' (cell addressing needs confirmation)."""
        self._require_confirmed("routing", unsafe)
        raw = encoding.gain_db_to_raw(gain_db)
        return self._write_word(self._addr(target), raw)

    # -- snapshot / restore ----------------------------------------------
    def snapshot(self, targets: List[AddrOrName]) -> Dict[str, bytes]:
        """Snapshot raw values for later restore (delegates to Device)."""
        return self.device.snapshot(targets)

    def restore(self, snap: Dict[str, bytes]) -> None:
        """Restore a snapshot produced by snapshot()."""
        self.device.restore(snap)
