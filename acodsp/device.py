"""device.py — high-level Device facade over Link + Protocol + ParamMap.

Setup switching uses the clean 0x1F config path only (never the 0x29 remote group,
which mutes). Writes default to SafeLoad. A snapshot/restore helper supports safe
hardware experiments.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Union

from acodsp import protocol
from acodsp.models import at01_for_model, model_for_pid
from acodsp.params import ParamMap
from acodsp.protocol import Protocol
from acodsp.transport import Link

AddrOrName = Union[int, str]


class ApplyError(RuntimeError):
    """Raised when a verified write fails readback (the snapshot is auto-restored first)."""


@dataclass
class Identity:
    """Parsed 0x08 identify response."""

    model: Optional[str]
    firmware: Optional[str]
    setup: Optional[int]
    raw: bytes


def _looks_like_addr(value: AddrOrName) -> bool:
    if isinstance(value, int):
        return True
    s = value.strip()
    return bool(re.fullmatch(r"(0x[0-9A-Fa-f]+|\d+)", s))


def _to_int(value: AddrOrName) -> int:
    if isinstance(value, int):
        return value
    return int(value, 0)


def _extract_ascii_runs(data: bytes, minlen: int = 3) -> List[str]:
    runs: List[str] = []
    cur = bytearray()
    for b in data:
        if 0x20 <= b < 0x7F:
            cur.append(b)
        else:
            if len(cur) >= minlen:
                runs.append(cur.decode("ascii"))
            cur = bytearray()
    if len(cur) >= minlen:
        runs.append(cur.decode("ascii"))
    return runs


class Device:
    """A connected (or dry-run) ACO DSP amplifier."""

    def __init__(
        self,
        link: Optional[Link] = None,
        param_map: Optional[ParamMap] = None,
        model: Optional[str] = None,
    ) -> None:
        self.link = link if link is not None else Link()
        self.proto = Protocol(self.link)
        self.params = param_map
        self.model_name = model
        self._channel_model = None

    # -- lifecycle ---------------------------------------------------------
    @classmethod
    def connect(
        cls,
        port: Optional[str] = None,
        dry_run: bool = False,
        model: Optional[str] = None,
        load_map: bool = True,
        verbose: bool = False,
    ) -> "Device":
        """Open the link (auto-detect by VID) and load the model's param map."""
        link = Link(dry_run=dry_run, verbose=verbose)
        link.open(port)
        resolved = model or model_for_pid(link.pid)
        param_map = None
        if load_map:
            basename = at01_for_model(resolved)
            if basename is not None:
                try:
                    param_map = ParamMap.load(basename)
                except FileNotFoundError:
                    param_map = None
        return cls(link=link, param_map=param_map, model=resolved)

    def close(self) -> None:
        self.link.close()

    def __enter__(self) -> "Device":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    # -- channel model (lazy) ---------------------------------------------
    @property
    def model(self) -> "ChannelModel":
        """The CHANNEL-MODEL abstraction (inputs / virtual / outputs / routing).

        Built lazily from the loaded ParamMap + the model's ``.channels.yaml`` overlay
        so usage reads like ``dev.model.output('A').gain(-3).delay_ms(2.5)``.
        """
        if self._channel_model is None:
            if self.params is None:
                raise RuntimeError("no param map loaded; cannot build the channel model")
            from acodsp.channels import ChannelModel

            basename = at01_for_model(self.model_name) or self.model_name
            self._channel_model = ChannelModel.load(basename, self.params, device=self)
        return self._channel_model

    # -- identify ----------------------------------------------------------
    def identify(self) -> Identity:
        """Send 0x08 identify and parse model / firmware / current setup."""
        raw = self.proto.identify()
        runs = _extract_ascii_runs(raw)
        model = runs[0] if runs else None
        firmware = None
        for r in runs:
            if re.search(r"\d{4}[.\-]\d", r):
                firmware = r
                break
        setup = None
        # The current setup is best read authoritatively via 0x28 0x40.
        return Identity(model=model, firmware=firmware, setup=setup, raw=raw)

    # -- setup switching (0x1F only) --------------------------------------
    def get_setup(self) -> Optional[int]:
        """Return the current 1-based setup, via GET 0x28 0x40."""
        resp = self.proto.get(protocol.GET_CURRENT_SETUP)
        # The wire is ASYMMETRIC (hardware-confirmed): the 0x1F SELECT command takes a
        # 0-based index (setup n -> n-1), but the 0x28/0x40 GET response byte is ALREADY
        # the 1-based setup number. So return resp[2] as-is — do NOT add 1 (that off-by-one
        # made readback report chosen+1 in both this CLI and the web client).
        if len(resp) >= 3 and resp[0] == protocol.OP_GET_GROUP and resp[1] == protocol.GET_CURRENT_SETUP:
            return resp[2]
        return None

    def set_setup(self, n: int) -> bytes:
        """Switch to 1-based setup *n* (idx n-1) via the clean 0x1F path (no mute)."""
        if not 1 <= n <= 10:
            raise ValueError("setup number must be 1-10 (firmware maps N -> index N-1)")
        return self.proto.select_setup(n - 1)

    # -- telemetry ---------------------------------------------------------
    def telemetry(self) -> bytes:
        """Return the raw 0x28 0x50 telemetry payload (supply voltage / temperature)."""
        return self.proto.get(protocol.GET_TELEMETRY)

    def manual_enable(self) -> Optional[int]:
        """Return the manual-enable flag (0x28 0x01); should stay 0 (no remote)."""
        resp = self.proto.get(protocol.GET_MANUAL_ENABLE)
        if len(resp) >= 3:
            return resp[2]
        return None

    # -- raw DSP param access ---------------------------------------------
    def _resolve_addr(self, target: AddrOrName) -> int:
        if _looks_like_addr(target):
            return _to_int(target)
        if self.params is None:
            raise RuntimeError("no param map loaded; pass a numeric address")
        return self.params.addr(str(target))

    def read_param(
        self, target: AddrOrName, nbytes: int = protocol.DEFAULT_WORD_SIZE
    ) -> bytes:
        """Read raw bytes from a DSP param by name or address (no value decoding)."""
        addr = self._resolve_addr(target)
        resp = self.proto.read_param(addr, nbytes=nbytes)
        # response echoes the header (02 <inst> <ah> <al>) then N value bytes.
        if len(resp) >= 4 and resp[0] == protocol.OP_DSP_READ:
            return resp[4:]
        return resp

    def write_param(
        self, target: AddrOrName, value: bytes, safeload: bool = True
    ) -> bytes:
        """Write raw big-endian bytes to a DSP param by name or address."""
        addr = self._resolve_addr(target)
        return self.proto.write_param(addr, bytes(value), safeload=safeload)

    # -- snapshot / restore (safe experiments) ----------------------------
    def snapshot(self, targets: List[AddrOrName], nbytes: int = protocol.DEFAULT_WORD_SIZE) -> Dict[str, bytes]:
        """Read a set of params and return {addr_key: raw_bytes} for later restore."""
        snap: Dict[str, bytes] = {}
        for t in targets:
            addr = self._resolve_addr(t)
            snap[hex(addr)] = self.read_param(addr, nbytes=nbytes)
        return snap

    def restore(self, snap: Dict[str, bytes], safeload: bool = True) -> None:
        """Write back a snapshot produced by snapshot()."""
        for addr_key, data in snap.items():
            self.write_param(int(addr_key, 0), data, safeload=safeload)

    # -- verified write (snapshot -> write -> readback -> restore-on-fail) --
    def write_verified(
        self,
        writes: "List[tuple]",
        snapshot: bool = True,
        verify: bool = True,
    ) -> Dict[str, bytes]:
        """Apply a batch of writes with snapshot/restore + readback verification.

        ``writes`` is a list of ``(target, value_bytes)`` where *value_bytes* is the
        already-encoded big-endian payload (4 bytes for a scalar, 20 for an EQ band).
        Each affected param is snapshotted (read) FIRST; all writes are emitted via the
        firmware SafeLoad path; then each is read back and compared. On ANY mismatch or
        error the whole snapshot is restored and :class:`ApplyError` is raised — so a
        failed apply never leaves the amp in a half-written state.

        Returns the snapshot dict (``{hex(addr): raw_bytes}``) on success.
        """
        # Snapshot the exact byte-width of each write so restore is faithful (the base
        # Device.snapshot reads a fixed 4 bytes; EQ bands are 20).
        snap: Dict[str, bytes] = {}
        if snapshot:
            for target, value in writes:
                addr = self._resolve_addr(target)
                key = hex(addr)
                if key not in snap:
                    snap[key] = self.read_param(addr, nbytes=len(value))
        try:
            for target, value in writes:
                self.write_param(target, bytes(value), safeload=True)
            if verify:
                for target, value in writes:
                    addr = self._resolve_addr(target)
                    got = self.read_param(addr, nbytes=len(value))
                    if bytes(got) != bytes(value):
                        raise ApplyError(
                            f"readback mismatch at {target} ({hex(addr)}): "
                            f"wrote {bytes(value).hex()} but read {bytes(got).hex()}"
                        )
        except Exception:
            if snapshot and snap:
                try:
                    self.restore(snap)
                except Exception:  # pragma: no cover - restore is best-effort
                    pass
            raise
        return snap

    # -- setup files (.pct6 / .afpx) --------------------------------------
    def apply_setup(self, setup, dry_run: bool = True, force: bool = False):
        """Apply a parsed :class:`acodsp.pct6.Setup` to this device's channel model.

        Dry-run by default (never transmits to real hardware). See
        :func:`acodsp.pct6.apply_setup` for the full safety + device-PID-match contract.
        """
        from acodsp.pct6 import apply_setup as _apply_setup

        return _apply_setup(self, setup, dry_run=dry_run, force=force)
