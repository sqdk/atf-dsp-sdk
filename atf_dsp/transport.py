"""transport.py — USB-CDC serial link and the ASYMMETRIC ACO framing.

Framing (verified from firmware FUN_0000481e / FUN_000048d0):
    host -> device (COMMAND):   0x42 | LEN=payload_len+1 | ~LEN | 0x01 | payload | SUM(payload)&0xFF
    device -> host (RESPONSE):  0x43 | LEN=payload_len   | ~LEN | 0x01 | payload | SUM(payload)&0xFF

`~LEN` is the ones-complement of LEN. `SUM` is the 8-bit sum of the payload bytes
only. `0x01` is a fixed type byte. Frames whose `~LEN` or checksum do not match are
rejected. Note the asymmetry: command LEN counts payload+1, response LEN counts
payload exactly.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import List, Optional

ATF_VID = 0x2E4F

CMD_START = 0x42   # 'B' — host -> device
RESP_START = 0x43  # 'C' — device -> host
TYPE_BYTE = 0x01   # fixed type byte

# Serial defaults. Baud is irrelevant on USB-CDC; 115200 is a harmless default.
DEFAULT_BAUD = 115200
DEFAULT_TIMEOUT = 0.3
# Opening a macOS cu.usbmodem toggles DTR and can drop the first read.
SETTLE_SECONDS = 0.25
READ_RETRIES = 3


def checksum(payload: bytes) -> int:
    """8-bit additive checksum of the payload bytes (SUM(payload) & 0xFF)."""
    return sum(payload) & 0xFF


def build_frame(payload: bytes, type_byte: int = TYPE_BYTE) -> bytes:
    """Wrap a payload in a host->device COMMAND frame (start 0x42, LEN=payload+1)."""
    length = (len(payload) + 1) & 0xFF
    header = bytes([CMD_START, length, (~length) & 0xFF, type_byte])
    return header + bytes(payload) + bytes([checksum(payload)])


@dataclass(frozen=True)
class Frame:
    """A decoded ACO frame."""

    direction: str          # "TX" (0x42 host->device) or "RX" (0x43 device->host)
    type_byte: int
    payload: bytes
    checksum_ok: bool
    offset: int             # byte offset within the parsed buffer
    raw: bytes


def parse_frames(buf: bytes) -> List[Frame]:
    """Decode every valid ACO frame in *buf*, in either direction.

    Handles both 0x42 command frames (payload = LEN-1) and 0x43 response frames
    (payload = LEN). Validates the ~LEN complement and the checksum; frames with a
    bad checksum or truncated tail are skipped.
    """
    out: List[Frame] = []
    i = 0
    n = len(buf)
    while i + 5 <= n:  # start,len,~len,type + >=0 payload + sum
        start = buf[i]
        if start in (CMD_START, RESP_START) and ((buf[i + 1] ^ 0xFF) & 0xFF) == buf[i + 2]:
            length = buf[i + 1]
            plen = length - 1 if start == CMD_START else length  # asymmetric length rule
            end = i + 5 + plen
            if plen >= 0 and end <= n:
                raw = bytes(buf[i:end])
                payload = raw[4:4 + plen]
                ck_ok = checksum(payload) == raw[-1]
                if ck_ok:
                    out.append(
                        Frame(
                            direction="TX" if start == CMD_START else "RX",
                            type_byte=raw[3],
                            payload=payload,
                            checksum_ok=True,
                            offset=i,
                            raw=raw,
                        )
                    )
                    i = end
                    continue
        i += 1
    return out


def find_atf_ports():
    """Return pyserial ListPortInfo entries whose USB VID is Audiotec Fischer."""
    from serial.tools import list_ports

    return [p for p in list_ports.comports() if getattr(p, "vid", None) == ATF_VID]


class Link:
    """USB-CDC serial link to an ACO device, with ACO framing and a dry-run mode.

    In dry-run mode no port is opened; frames are built (and printed) but never
    transmitted, and `command()` returns b"".
    """

    def __init__(
        self,
        dry_run: bool = False,
        baud: int = DEFAULT_BAUD,
        timeout: float = DEFAULT_TIMEOUT,
        verbose: bool = False,
        mem: Optional[dict] = None,
        stuck_addrs: Optional[set] = None,
    ) -> None:
        self.dry_run = dry_run
        self.baud = baud
        self.timeout = timeout
        self.verbose = verbose
        self._serial = None
        self.port: Optional[str] = None
        self.pid: Optional[int] = None
        # Frames built by command(), captured for dry-run inspection / tests.
        self.sent: List[bytes] = []
        # Memory-backed dry-run: when `mem` is a dict {addr:int -> 4-byte word}, the
        # dry-run link SIMULATES a DSP — 0x02 reads return the stored word(s) and 0x03
        # writes update the store. This turns dry-run from a silent b"" sink into a
        # round-trippable fake device, so from_device / snapshot-restore / readback-verify
        # are fully testable OFFLINE. `stuck_addrs` (addresses whose writes are dropped)
        # simulates a cell that won't take a write, to exercise the readback-verify + restore path.
        self.mem: Optional[dict] = mem
        self.stuck_addrs: set = set(stuck_addrs or ())

    # -- lifecycle ---------------------------------------------------------
    def open(self, port: Optional[str] = None) -> "Link":
        """Open the serial port, auto-detecting by VID 0x2E4F when *port* is None.

        In dry-run mode this records the (possibly detected) port name without
        opening anything.
        """
        if port is None:
            hits = find_atf_ports()
            if not hits:
                if self.dry_run:
                    self.port = None
                    return self
                raise RuntimeError(
                    "No ATF device (VID 0x2E4F) found as a serial port. "
                    "Is the amp powered and connected?"
                )
            port = hits[0].device
            self.pid = getattr(hits[0], "pid", None)
        self.port = port
        if self.dry_run:
            return self
        import serial

        self._serial = serial.Serial(port, self.baud, timeout=self.timeout)
        # Settle: opening a cu.usbmodem toggles DTR and can drop the first read.
        time.sleep(SETTLE_SECONDS)
        try:
            self._serial.reset_input_buffer()
        except OSError:
            pass
        return self

    def close(self) -> None:
        if self._serial is not None:
            try:
                self._serial.close()
            finally:
                self._serial = None

    def __enter__(self) -> "Link":
        if self._serial is None and not self.dry_run:
            self.open(self.port)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    @property
    def is_open(self) -> bool:
        return self._serial is not None

    # -- framing convenience ----------------------------------------------
    @staticmethod
    def build_frame(payload: bytes, type_byte: int = TYPE_BYTE) -> bytes:
        return build_frame(payload, type_byte)

    @staticmethod
    def parse_frames(buf: bytes) -> List[Frame]:
        return parse_frames(buf)

    # -- transaction ------------------------------------------------------
    def command(self, payload: bytes, read_timeout: float = 0.3) -> bytes:
        """Send a command payload and return the first response payload (b"" if none).

        In dry-run mode the frame is built (and printed) but not transmitted, and
        b"" is returned.
        """
        frame = build_frame(payload)
        self.sent.append(frame)
        if self.dry_run:
            if self.verbose:
                print(f"[dry-run] TX {_hexs(frame)}")
            if self.mem is not None:
                return self._simulate(payload)
            return b""
        if self._serial is None:
            raise RuntimeError("Link is not open. Call open() first.")
        if self.verbose:
            print(f"[>] TX {_hexs(frame)}")
        self._serial.reset_input_buffer()
        self._serial.write(frame)
        self._serial.flush()

        buf = bytearray()
        deadline = time.monotonic() + read_timeout
        # Settle + retry the first read (macOS cu.usbmodem can drop it).
        for _ in range(READ_RETRIES):
            while time.monotonic() < deadline:
                # Read only what's actually buffered instead of read(256), which would block for the
                # full port timeout waiting for 256 bytes that never come (responses are 10-140 B).
                # read(1) returns the instant the first byte lands (device turnaround, ~ms); the next
                # iteration drains in_waiting. ~10-50x faster per command over the whole round-trip.
                n = self._serial.in_waiting
                chunk = self._serial.read(n) if n else self._serial.read(1)
                if chunk:
                    buf += chunk
                    frames = [f for f in parse_frames(buf) if f.direction == "RX"]
                    if frames:
                        if self.verbose:
                            print(f"[<] RX {_hexs(bytes(buf))}")
                        return frames[0].payload
                else:
                    time.sleep(0.02)
            if buf:
                break
            deadline = time.monotonic() + read_timeout
        return b""

    # -- memory-backed dry-run simulation ---------------------------------
    def _mem_read(self, addr: int, nbytes: int) -> bytes:
        """Assemble `nbytes` from the word store (4-byte words at consecutive addrs)."""
        out = bytearray()
        word = addr
        while len(out) < nbytes:
            out += self.mem.get(word, b"\x00\x00\x00\x00")
            word += 1
        return bytes(out[:nbytes])

    def _mem_write(self, addr: int, data: bytes) -> None:
        """Store `data` as consecutive 4-byte words from `addr` (dropping stuck cells)."""
        for i in range(0, len(data), 4):
            word_addr = addr + i // 4
            if word_addr in self.stuck_addrs:
                continue  # simulate a cell that won't accept the write
            self.mem[word_addr] = bytes(data[i:i + 4]).ljust(4, b"\x00")

    def _simulate(self, payload: bytes) -> bytes:
        """Simulate a DSP response for a 0x02 read / 0x03 write payload (memory-backed)."""
        if not payload:
            return b""
        op = payload[0]
        if op == 0x02 and len(payload) >= 4:  # 02 inst ah al [pad]
            addr = (payload[2] << 8) | payload[3]
            nbytes = max(4, len(payload) - 4)
            return bytes(payload[:4]) + self._mem_read(addr, nbytes)
        if op == 0x03 and len(payload) >= 5:  # 03 inst flag ah al data...
            addr = (payload[3] << 8) | payload[4]
            self._mem_write(addr, payload[5:])
            return bytes(payload[:5])
        return b""


def _hexs(b: bytes) -> str:
    return " ".join(f"{x:02X}" for x in b)
