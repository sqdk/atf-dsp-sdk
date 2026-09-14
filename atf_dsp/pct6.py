"""pct6.py — read/write Audiotec Fischer DSP PC-Tool setup files (.pct6 / .afpx).

This module carries three layers, kept deliberately separate:

1. ``_Pct6Codec`` — the device-agnostic CRYPTO/CONTAINER transform, ported from the
   reverse-engineered ``ATF_DSP_PC-TOOL_6.exe`` (see ``docs/pct6-format.md``, Phases
   A+B, CONFIRMED by a self-proving decrypt of the shipped sample):

       plaintext_xml = qUncompress( XOR(whole_file_bytes, KEY) )

   * XOR is a plain repeating-key stream over the ENTIRE file — no header/IV/salt/pad.
   * ``qUncompress`` = Qt's format: a 4-byte BIG-ENDIAN uncompressed length followed by
     a raw zlib stream. Writing is the exact inverse (``XOR(qCompress(xml), KEY)``).
   * KEY selects the variant: ``ATFV6`` (standard ``.pct6``), ``ATF`` (legacy ``.afpx``).
     ``ATFV6P`` (password-protected) uses an unconfirmed ``QCryptographicHash`` scheme
     that is deliberately NOT implemented — a clear error is raised instead of a guess.

2. ``Setup`` (+ dataclasses ``SetupMetadata``, ``Channel``, ``EqBand``, ``RoutingBlock``,
   ``Routing``) — the structured model parsed from the ``<ATF>`` XML container.

3. ``apply_setup`` — maps a parsed ``Setup`` onto a connected (or dry-run) ``Device``'s
   channel model via the hardware-confirmed typed API (gain / EQ). See its docstring for
   the device-PID match caveat.

Nothing here reinvents DSP math: gain/EQ writes go through the confirmed encoders in
:mod:`atf_dsp.encoding` / :mod:`atf_dsp.controls` / :mod:`atf_dsp.channels`.
"""
from __future__ import annotations

import math
import struct
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Dict, List, Optional, Tuple

import xml.etree.ElementTree as ET

if TYPE_CHECKING:  # pragma: no cover - typing only
    from atf_dsp.device import Device

# --- crypto/container variant keys (compile-time constants in the exe) ---------
KEY_PCT6 = b"ATFV6"        # standard .pct6                (literal @ 0x028898a0)
KEY_PCT6_PW = b"ATFV6P"    # password-protected .pct6      (unconfirmed — not supported)
KEY_AFPX = b"ATF"          # legacy .afpx                  (literal @ 0x028551dc)

# Ordered plain-XOR variants we CAN decrypt (ATFV6P is intentionally excluded).
_PLAIN_XOR_KEYS: Tuple[bytes, ...] = (KEY_PCT6, KEY_AFPX)


class Pct6Error(RuntimeError):
    """Raised when a .pct6/.afpx file cannot be decrypted or parsed."""


class Pct6PasswordError(Pct6Error):
    """Raised for a password-protected (ATFV6P) file — its scheme is unconfirmed."""


# ---------------------------------------------------------------------------
# 1) crypto / container codec
# ---------------------------------------------------------------------------
def _xor(data: bytes, key: bytes) -> bytes:
    """Repeating-key XOR over the whole buffer: ``out[i] = data[i] ^ key[i % len]``."""
    klen = len(key)
    return bytes(b ^ key[i % klen] for i, b in enumerate(data))


def q_uncompress(data: bytes) -> bytes:
    """Qt ``qUncompress``: 4-byte big-endian length + raw zlib stream -> plaintext."""
    if len(data) < 4:
        raise ValueError("too short for a qCompress header")
    declared = struct.unpack(">I", data[:4])[0]
    out = zlib.decompress(data[4:])
    if len(out) != declared:
        raise ValueError(f"qUncompress length mismatch: {len(out)} != {declared}")
    return out


def q_compress(data: bytes, level: int = 6) -> bytes:
    """Qt ``qCompress``: 4-byte big-endian length + raw zlib stream. Inverse of
    :func:`q_uncompress` (byte-for-byte lossless through :func:`zlib.decompress`)."""
    return struct.pack(">I", len(data)) + zlib.compress(data, level)


class _Pct6Codec:
    """The device-agnostic (de)obfuscation transform. Stateless — methods are static."""

    @staticmethod
    def decrypt(file_bytes: bytes) -> Tuple[str, bytes]:
        """Return ``(variant, xml_bytes)`` for a ``.pct6``/``.afpx`` file.

        Tries: plain ``qCompress`` (no XOR), then each supported XOR key, then a raw
        (uncompressed) XML fallback. Raises :class:`Pct6PasswordError` when the file
        looks like an unsupported password-protected (``ATFV6P``) container, and
        :class:`Pct6Error` when no known variant applies.
        """
        # 1) plain qCompress (no obfuscation layer)
        try:
            return "plain", q_uncompress(file_bytes)
        except Exception:
            pass
        # 2) each supported repeating-XOR key
        for key in _PLAIN_XOR_KEYS:
            try:
                return f"XOR:{key.decode()}", q_uncompress(_xor(file_bytes, key))
            except Exception:
                continue
        # 3) raw, uncompressed XML (last resort)
        if file_bytes.lstrip()[:1] == b"<":
            return "raw", file_bytes
        # Nothing worked. A password-protected file XORs cleanly with the ATFV6P
        # literal but its payload is further transformed via QCryptographicHash, so
        # q_uncompress still fails — surface that as a clear, honest error.
        if _looks_password_protected(file_bytes):
            raise Pct6PasswordError(
                "file appears to be a password-protected .pct6 (ATFV6P); that scheme is "
                "unconfirmed and not supported — open it once in the PC-Tool and re-save "
                "without a password, or supply a decrypted export."
            )
        raise Pct6Error("no known .pct6/.afpx variant could decrypt this file")

    @staticmethod
    def encrypt(xml_bytes: bytes, variant: str = "XOR:ATFV6") -> bytes:
        """Serialize ``xml_bytes`` back to a container. The exact inverse of
        :meth:`decrypt` for the ``plain`` / ``XOR:*`` / ``raw`` variants."""
        if variant == "raw":
            return xml_bytes
        compressed = q_compress(xml_bytes)
        if variant == "plain":
            return compressed
        if variant.startswith("XOR:"):
            key = variant.split(":", 1)[1].encode()
            if key == KEY_PCT6_PW:
                raise Pct6PasswordError("cannot write a password-protected (ATFV6P) file")
            return _xor(compressed, key)
        raise Pct6Error(f"unknown container variant: {variant!r}")


def _looks_password_protected(file_bytes: bytes) -> bool:
    """Heuristic: after XOR-ing with the ATFV6P literal the length header is plausible
    but the zlib stream does not inflate. We only use this to give a helpful message."""
    try:
        candidate = _xor(file_bytes, KEY_PCT6_PW)
        struct.unpack(">I", candidate[:4])
        zlib.decompress(candidate[4:])
        return False  # it actually inflated -> not the pw case
    except zlib.error:
        return True
    except Exception:
        return False


# Public convenience wrappers ------------------------------------------------
def decrypt(file_bytes: bytes) -> Tuple[str, bytes]:
    """Decrypt raw ``.pct6``/``.afpx`` bytes to ``(variant, xml_bytes)``."""
    return _Pct6Codec.decrypt(file_bytes)


def encrypt(xml_bytes: bytes, variant: str = "XOR:ATFV6") -> bytes:
    """Re-encrypt XML bytes to container bytes for the given variant."""
    return _Pct6Codec.encrypt(xml_bytes, variant)


# ---------------------------------------------------------------------------
# 2) the Setup model
# ---------------------------------------------------------------------------
# <Fil T=...> filter-type codes. Mapped from the shipped sample + the HPi/LPi index
# correspondence (an output's LPi points at its lowpass <Fil>, HPi at its highpass).
#   1  -> peaking  : graphic-EQ band (fixed ISO centre freq, Q=4.3)                CONFIRMED
#   17 -> peaking  : parametric peaking band (user freq/gain/Q)                    CONFIRMED
#   9  -> lowpass  : lowpass crossover  (an output's LPi indexes this band)        INFERRED
#   10 -> highpass : highpass crossover (an output's HPi indexes this band)        INFERRED
# Shelf codes (low/high shelf) and any others are NOT present in the sample and are
# deliberately left unmapped: their kind is None and ``raw_type`` is preserved.
# <Fil T> filter-type codes. Peaking = 1/17. Crossovers encode the CHARACTERISTIC in the code:
# Butterworth LP=9 / HP=10, Linkwitz-Riley LP=15 / HP=16 (confirmed vs the real M54 file + PC-Tool).
FIL_TYPE_MAP: Dict[int, str] = {1: "peaking", 17: "peaking",
                                9: "lowpass", 10: "highpass", 15: "lowpass", 16: "highpass"}
# crossover code -> characteristic (for round-tripping the characteristic on load)
FIL_XOVER_CHARACTERISTIC: Dict[int, str] = {9: "butterworth", 10: "butterworth",
                                            15: "linkwitz_riley", 16: "linkwitz_riley"}

# CN (channel name/role) -> display name. Recovered from the PC-Tool exe: CN keys a
# std::map<int, ChannelSetup> (builder FUN_005f0410 @0x5f0410, lookup @0x5ef5d0) whose
# descriptor is composed as "[type] [position] [side] [band] [number]" (composer @0x5f3e30).
# 53 entries; VALIDATED against the real UP-8BMW setup (inputs CN 1/2/14/15/26/27/38/39/54/55
# all matched). CN 13 & 25 appear in v6.03.03-authored (migrated) setups but are ABSENT from the
# analyzed v6.04.01 map (enum pruned between versions) — we don't assert a name for them
# ("Unknown"). A CN not in this table -> "Not assigned".
CN_NAMES: Dict[int, str] = {
    0: "Not assigned",
    1: "Front Left", 2: "Front Right",
    3: "Front Left High", 4: "Front Right High",
    5: "Front Left Mid", 6: "Front Right Mid",
    7: "Front Left Low", 8: "Front Right Low",
    9: "Front Center", 10: "Front Center",
    11: "Front Center High", 12: "Front Center Low",
    13: "Unknown (CN13)",  # absent from analyzed v6.04.01 map — not asserted
    14: "Rear Left", 15: "Rear Right",
    16: "Rear Left High", 17: "Rear Right High",
    18: "Rear Left Mid", 19: "Rear Right Mid",
    20: "Rear Left Low", 21: "Rear Right Low",
    22: "Rear Center", 23: "Rear Center High", 24: "Rear Center Low",
    25: "Unknown (CN25)",  # absent from analyzed v6.04.01 map — not asserted
    26: "Subwoofer 1", 27: "Subwoofer 2", 28: "Subwoofer 3", 29: "Subwoofer 4",
    30: "Line In 1", 31: "Line In 2", 32: "Line In 3", 33: "Line In 4",
    34: "Line In 5", 35: "Line In 6", 36: "Line In 7", 37: "Line In 8",
    38: "Digital In Left", 39: "Digital In Right",
    54: "AUX In Left 1", 55: "AUX In Right 1", 64: "AUX In Left 2", 65: "AUX In Right 2",
    66: "USB In Front Left", 67: "USB In Front Right",
    68: "USB In Rear Left", 69: "USB In Rear Right",
    70: "Left Subwoofer", 71: "Right Subwoofer",
    72: "Surround Left", 73: "Surround Right",
    74: "Front Left Subwoofer", 75: "Front Right Subwoofer",
    76: "USB Left", 77: "USB Right",
}
CN_NAMES_UNKNOWN = frozenset({13, 25})    # present in migrated setups, absent from the analyzed map
CN_NAMES_INFERRED = CN_NAMES_UNKNOWN      # backward-compat alias


def _as_int(value: Optional[str]) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except ValueError:
        try:
            return int(float(value))
        except ValueError:
            return None


def _as_float(value: Optional[str]) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _as_bool(value: Optional[str]) -> Optional[bool]:
    i = _as_int(value)
    return None if i is None else bool(i)


def _level_to_db(level: Optional[float]) -> Optional[float]:
    """A ``<Vol L=...>`` linear level multiplier -> gain in dB (None if absent/<=0)."""
    if level is None or level <= 0:
        return None
    return 20.0 * math.log10(level)


@dataclass
class EqBand:
    """One ``<Fil>`` EQ/filter band. Bands are stored low->high frequency."""

    freq: float
    gain_db: float
    q: float
    kind: Optional[str]          # mapped filter kind (peaking/lowpass/highpass) or None
    raw_type: int                # original <Fil T=...> code (preserved for unmapped kinds)
    bypass: bool
    index: int                   # <Fil I=...>
    raw: Dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_element(cls, el: ET.Element) -> "EqBand":
        raw = dict(el.attrib)
        t = _as_int(raw.get("T")) or 0
        return cls(
            freq=_as_float(raw.get("F")) or 0.0,
            gain_db=_as_float(raw.get("G")) or 0.0,
            q=_as_float(raw.get("Q")) or 0.0,
            kind=FIL_TYPE_MAP.get(t),
            raw_type=t,
            bypass=bool(_as_bool(raw.get("FilBy"))),
            index=_as_int(raw.get("I")) or 0,
            raw=raw,
        )


@dataclass
class Channel:
    """One output (``<OC>``) or input (``<IC>``) channel.

    Confidently-mapped fields (name/gain/mute/eq_bypass/eq_bands/crossover indices) are
    surfaced with friendly names; everything else stays in ``raw`` so no information is
    lost and nothing is invented. ``name`` is the role label resolved from the ``CN``
    code via ``CN_NAMES`` (the exe's channel-name map), or an explicit user string if the
    setup carries one. ``name_code`` keeps the raw ``CN`` (see ``CN_NAMES_UNKNOWN`` for
    codes present in migrated setups but not in the analyzed map).

    MUTE vs the ``MT`` attribute (verified against the PC-Tool + a real M 5.4DSP file):
    ``MT`` is **not** a mute flag — it is an ASSIGNED/populated flag (real assigned
    outputs carry ``MT=1``, unassigned ``CN=0`` outputs ``MT=0``; inputs are ``MT=0``).
    It is surfaced as :attr:`assigned`. Actual MUTE shares the DSP gain cell with gain,
    so in ``.pct6`` it is represented by the linear ``<Vol L>`` level being ``<= 0``
    (0 == muted); :attr:`mute` is derived from that, not from ``MT``."""

    kind: str                    # "output" | "input"
    index: int                   # 0-based position within its class
    name: Optional[str]          # friendly text label, if the setup provides one
    name_code: Optional[int]     # CN attribute (numeric name/label reference)
    gain_db: Optional[float]     # from the <Vol L=...> linear level, if present
    mute: Optional[bool]         # gain/mute share the DSP cell: <Vol L> <= 0 == muted
    assigned: Optional[bool]     # MT — channel is populated/assigned (NOT mute; see note)
    enabled: Optional[bool]      # CE (outputs only)
    eq_bypass: Optional[bool]    # EqBy
    eq_bands: List[EqBand]
    hp_index: Optional[int]      # HPi — index of the highpass crossover <Fil> (or out-of-range = off)
    lp_index: Optional[int]      # LPi — index of the lowpass crossover <Fil>
    delay_raw: Optional[Dict[str, str]]  # the <T> child (phase/delay; unit unconfirmed -> raw)
    raw: Dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_element(cls, el: ET.Element, index: int, kind: str) -> "Channel":
        raw = dict(el.attrib)
        vol = el.find("Vol")
        level = _as_float(vol.get("L")) if vol is not None else None
        t_el = el.find("T")
        cn = _as_int(raw.get("CN"))
        return cls(
            kind=kind,
            index=index,
            # explicit user string if present, else the CN role name from the exe table
            name=raw.get("Name") or raw.get("NM") or CN_NAMES.get(cn),
            name_code=cn,
            gain_db=_level_to_db(level),
            # mute shares the DSP gain cell: a <Vol L> level of 0 (or absent-and-<=0)
            # is muted. MT is the ASSIGNED flag, not mute (see the class docstring).
            mute=(None if level is None else level <= 0.0),
            assigned=_as_bool(raw.get("MT")),
            enabled=_as_bool(raw.get("CE")),
            eq_bypass=_as_bool(raw.get("EqBy")),
            eq_bands=[EqBand.from_element(f) for f in el.findall("Fil")],
            hp_index=_as_int(raw.get("HPi")),
            lp_index=_as_int(raw.get("LPi")),
            delay_raw=dict(t_el.attrib) if t_el is not None else None,
            raw=raw,
        )

    def crossover_bands(self) -> Dict[str, Optional[EqBand]]:
        """Return ``{'highpass': band|None, 'lowpass': band|None}`` resolved from the
        HPi/LPi indices (which point into ``eq_bands``; out-of-range = crossover off)."""
        n = len(self.eq_bands)
        hp = self.eq_bands[self.hp_index] if self.hp_index is not None and 0 <= self.hp_index < n else None
        lp = self.eq_bands[self.lp_index] if self.lp_index is not None and 0 <= self.lp_index < n else None
        return {"highpass": hp, "lowpass": lp}


@dataclass
class RoutingBlock:
    """One ``<R>`` routing block: an ``INS`` x ``OUTS`` grid of ``G<n>`` mix gains
    (linear coefficients), plus ``AOM``/``OFFS`` metadata (meaning not yet confirmed)."""

    index: int
    ins: Optional[int]
    outs: Optional[int]
    offs: Optional[int]
    aom: Optional[int]
    gains: Dict[str, float]      # {"G0": 1.0, "G62": 0.5, ...} — all cells, source order
    raw: Dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_element(cls, el: ET.Element, index: int) -> "RoutingBlock":
        raw = dict(el.attrib)
        gains = {k: (_as_float(v) or 0.0) for k, v in raw.items() if k.startswith("G") and k[1:].isdigit()}
        return cls(
            index=index,
            ins=_as_int(raw.get("INS")),
            outs=_as_int(raw.get("OUTS")),
            offs=_as_int(raw.get("OFFS")),
            aom=_as_int(raw.get("AOM")),
            gains=gains,
            raw=raw,
        )


@dataclass
class Routing:
    """All ``<R>`` blocks under ``<Route>`` (the setup's routing matrices)."""

    blocks: List[RoutingBlock] = field(default_factory=list)


@dataclass
class SetupMetadata:
    """The ``<ATF>`` root attributes.

    ``pid`` holds the ``Dev`` attribute — the PC-Tool's INTERNAL device-type id (e.g. 29
    for the MATCH M 5.4DSP, 50 for the UP 8BMW). This is NOT the USB PID (0x2008 for the
    M 5.4DSP): the two are different number spaces (see :data:`atf_dsp.models.DEV_IDS` vs
    ``PID_MODELS``). The device-match check in :func:`apply_setup` compares this ``Dev``
    against the connected device's OWN internal ``Dev`` id (``dev_id_for_model``), never
    against the USB PID. (The attribute is named ``pid`` for historical reasons only.)"""

    pid: Optional[int]           # Dev — internal device-type id, NOT the USB PID
    version: Optional[str]       # V (tool version)
    filename: Optional[str]      # FN (original path)
    outs: Optional[int]          # OUTS
    ins: Optional[int]           # INS
    raw: Dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_root(cls, root: ET.Element) -> "SetupMetadata":
        raw = dict(root.attrib)
        return cls(
            pid=_as_int(raw.get("Dev")),
            version=raw.get("V"),
            filename=raw.get("FN"),
            outs=_as_int(raw.get("OUTS")),
            ins=_as_int(raw.get("INS")),
            raw=raw,
        )


class Setup:
    """A parsed DSP PC-Tool setup: metadata + per-channel outputs/inputs + routing.

    Construct via :meth:`load` (decrypt + parse a file) or :meth:`from_xml` (parse
    already-decrypted XML). :meth:`save` / :meth:`to_xml` re-encrypt the retained XML
    tree (Phase D) — the exact inverse of load for an unmodified setup.
    """

    def __init__(
        self,
        metadata: SetupMetadata,
        virtuals: List[Channel],
        outputs: List[Channel],
        inputs: List[Channel],
        routing: Routing,
        variant: str,
        root: ET.Element,
    ) -> None:
        self.metadata = metadata
        # <OC> elements split by class: virtual channels (no crossover) lead the list,
        # physical outputs (carry HPi/LPi crossover slots) follow. Each re-indexed 0.. -> A..
        self.virtuals = virtuals
        self.outputs = outputs
        self.inputs = inputs
        self.routing = routing
        self.variant = variant
        # The original parsed tree is retained for lossless re-serialisation (save()).
        # It is excluded from equality so two Setups compare by their parsed model.
        self._root = root

    # -- construction -----------------------------------------------------
    @classmethod
    def from_xml(cls, xml_bytes: bytes, variant: str = "XOR:ATFV6") -> "Setup":
        """Parse already-decrypted ``<ATF>`` XML bytes into a Setup."""
        root = ET.fromstring(xml_bytes)
        if root.tag != "ATF":
            raise Pct6Error(f"unexpected XML root <{root.tag}>; expected <ATF>")
        metadata = SetupMetadata.from_root(root)
        # <OC> holds BOTH virtual channels and physical outputs, virtuals first. Discriminate by
        # crossover presence: physical outputs carry HPi/LPi slots; virtual channels do not.
        # (hw-confirmed: M54 = 9 virtual + 9 output; UP-8BMW = 11 virtual + 9 output — so the split
        # is NOT simply OUTS/2.)
        virtuals: List[Channel] = []
        outputs: List[Channel] = []
        for el in root.findall("OC"):
            if el.get("HPi") is not None:        # physical output (has a crossover)
                outputs.append(Channel.from_element(el, len(outputs), "output"))
            else:                                # virtual (tuning) channel
                virtuals.append(Channel.from_element(el, len(virtuals), "virtual"))
        inputs = [Channel.from_element(el, i, "input")
                  for i, el in enumerate(root.findall("IC"))]
        route_el = root.find("Route")
        blocks = ([RoutingBlock.from_element(r, i) for i, r in enumerate(route_el.findall("R"))]
                  if route_el is not None else [])
        return cls(metadata, virtuals, outputs, inputs, Routing(blocks), variant, root)

    @classmethod
    def load(cls, path: "str | Path") -> "Setup":
        """Decrypt and parse a ``.pct6``/``.afpx`` file into a Setup."""
        data = Path(path).read_bytes()
        variant, xml_bytes = _Pct6Codec.decrypt(data)
        return cls.from_xml(xml_bytes, variant=variant)

    @classmethod
    def from_device(cls, device: "Device") -> "Setup":
        """Read the live tuning state off *device* and assemble a PC-Tool-loadable Setup (RE-1).

        Reads, per channel, the params the ``.pct6`` model carries: per-channel gain/mute
        (``MOD_OUTPUTMUTE``/``MOD_VCPMUTE``/``MOD_INPUTGAIN`` cells), the EQ bands (recovered
        from their stored biquad coefficients via :func:`encoding.peaking_from_coeffs`),
        output delays, and both routing matrices. Builds an ``<ATF>`` XML tree that
        ``save()``s to a file loadable in the PC-Tool and that round-trips through
        :meth:`load`.

        HONEST SCOPE (PoC / offline): fully recovered = gain/mute, peaking EQ (output +
        virtual graphic + input parametric — the exact analytic biquad inverse), delay, and
        routing. Output CROSSOVERS are now recovered BEST-EFFORT: the FILTERS HP/LP stage
        coefficients are inverted to a corner frequency + slope (from the active-stage count)
        + a characteristic inferred from the corner-stage Q (see
        :meth:`atf_dsp.channels.OutputChannel.recover_crossover` — corner/slope are exact, the
        characteristic is an inference against the confirmed ``_XOVER_Q`` table). The recovered
        HP/LP appear as ``<Fil T=10/9>`` bands with HPi/LPi pointing at them; an off section is
        emitted out-of-range. NOT recovered (a PC-Tool setup concept, not a DSP RAM param): the
        channel ROLE code ``CN`` — set to 0 / "Not assigned". Everything read is a ``0x02``
        read — this method never writes to the amp.
        """
        return _from_device(device)

    # -- serialisation (Phase D) ------------------------------------------
    def to_xml(self) -> bytes:
        """Serialise the retained XML tree back to UTF-8 bytes (no XML declaration,
        matching the PC-Tool's container payload)."""
        return ET.tostring(self._root, encoding="unicode").encode("utf-8")

    def save(self, path: "str | Path", variant: Optional[str] = None) -> None:
        """Re-encrypt the setup to a ``.pct6``/``.afpx`` file. Uses the variant the
        file was loaded with unless ``variant`` overrides it."""
        blob = _Pct6Codec.encrypt(self.to_xml(), variant or self.variant)
        Path(path).write_bytes(blob)

    # -- convenience ------------------------------------------------------
    def summary(self) -> str:
        """A short human summary (counts, first output's first EQ band)."""
        first_band = self.outputs[0].eq_bands[0] if self.outputs and self.outputs[0].eq_bands else None
        band_txt = (f"{first_band.freq:g}Hz/{first_band.gain_db:g}dB/Q{first_band.q:g}"
                    if first_band else "n/a")
        return (
            f"Setup(dev={self.metadata.pid}, v={self.metadata.version}, "
            f"virtuals={len(self.virtuals)}, outputs={len(self.outputs)}, ins={len(self.inputs)}, "
            f"routing_blocks={len(self.routing.blocks)}, first_output_band={band_txt})"
        )

    def unmapped_filter_types(self) -> List[int]:
        """Sorted distinct ``<Fil T=...>`` codes present that we could NOT map to a kind."""
        seen = set()
        for ch in (*self.virtuals, *self.outputs, *self.inputs):
            for band in ch.eq_bands:
                if band.kind is None:
                    seen.add(band.raw_type)
        return sorted(seen)

    # -- equality (model, not tree) ---------------------------------------
    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Setup):
            return NotImplemented
        return (
            self.metadata == other.metadata
            and self.virtuals == other.virtuals
            and self.outputs == other.outputs
            and self.inputs == other.inputs
            and self.routing == other.routing
        )

    def __repr__(self) -> str:
        return self.summary()


# ---------------------------------------------------------------------------
# 2b) reading a Setup off a live Device (RE-1)
# ---------------------------------------------------------------------------
def _read_gain_lin(device: "Device", name: str) -> float:
    """Read a gain/mute cell -> linear multiplier (0.0 == muted). Best-effort (0 on empty)."""
    from atf_dsp import encoding

    data = device.read_param(name, nbytes=4)
    if not data or len(data) < 4:
        return 1.0
    return encoding.from_fixed(int.from_bytes(data[:4], "big"))


def _read_polarity_inverted(device: "Device", oc) -> bool:
    """Read a physical output's polarity -> True when inverted. Polarity is the SIGN of the
    output gain/mute cell (hardware-confirmed 2026-08-30): a negative value == inverted. Feeds
    the .pct6 ``CINV`` field. Best-effort (normal on empty/unresolvable)."""
    from atf_dsp import encoding

    try:
        name = oc.resolve_gain().name          # polarity = the SIGN of the gain/mute cell
    except Exception:
        return False
    data = device.read_param(name, nbytes=4)
    if not data or len(data) < 4:
        return False
    return encoding.from_fixed(int.from_bytes(data[:4], "big")) < 0


def _read_eq_bands_xml(device: "Device", resolve, count: int,
                       freqs: List[float]) -> List["ET.Element"]:
    """Read *count* EQ bands via ``resolve(i) -> EqBandRef`` and emit low->high <Fil> els."""
    from atf_dsp import encoding

    els: List[ET.Element] = []
    for i in range(count):
        try:
            ref = resolve(i)
        except Exception:
            break
        data = device.read_param(ref.base, nbytes=20)
        words = [int.from_bytes(data[j:j + 4], "big") for j in range(0, min(len(data), 20), 4)]
        default_f = float(freqs[i]) if i < len(freqs) else 1000.0
        # classify peaking vs low/high-shelf (fixes the from_device/dsp_web bug where a shelf was
        # force-fit as a wrong peaking band — hardware-confirmed 2026-08-30).
        rec = encoding.eq_band_from_coeffs(words, fs=device.fs) if len(words) == 5 else None
        if rec is None:  # bypassed / flat / uninitialised
            kind, f, q, g, byp = "peaking", default_f, 4.318, 0.0, 1
        else:
            kind, f, q, g = rec
            byp = 0
        attrs = {"I": str(i), "T": "17", "F": f"{f:.2f}", "G": f"{g:.2f}",
                 "Q": f"{q:.4f}", "FilBy": str(byp)}
        if kind != "peaking":
            # shelves recover with correct f/Q/gain; the exact .pct6 <Fil T> shelf code is not yet
            # captured, so tag the kind for consumers (dsp_web) rather than mis-encode T.
            attrs["Kind"] = kind
        els.append(ET.Element("Fil", attrs))
    return els


def _oc_el(cn: int, gain_lin: float, eq_els: List["ET.Element"], *, on: int,
          finit: int, hp_lp: Optional[Tuple[int, int]] = None,
          delay_samples: int = 0, cinv: int = 0) -> "ET.Element":
    """Build one ``<OC>`` (virtual or physical output) matching the REAL PC-Tool schema
    for a POPULATED channel (verified against a real M 5.4DSP ``.pct6``).

    Attribute schema (child order Fil*, Vol, T, CHS):
      * ``ON``   — the channel's slot index in the OUTS space (virtuals 0..N-1, physical
                   outputs N.. where N = number of virtuals).
      * ``CN``   — role code (0 == "Not assigned"; a PC-Tool concept, not recoverable
                   from DSP RAM, so from_device leaves it 0).
      * ``MT=1`` — ASSIGNED/populated flag (NOT mute). from_device only emits populated
                   channels, so this is always 1 — this is the fix that makes the PC-Tool
                   show our gain/delay instead of treating the channel as unassigned.
      * ``CE=1`` — channel enabled.
      * ``LG``   — link group (0 == none; group membership is not recovered).
      * ``Finit``— number of graphic/parametric EQ bands (excludes crossover filters).
      * ``CINV`` — output polarity (0 == normal, 1 == inverted). Read per-output from the DSP
                   polarity cell (PROVISIONAL ±1.0 encoding); virtuals have no polarity cell so
                   they pass ``cinv=0``.
      * ``DG=0``, ``GD=0`` — DSP/group flags (0 in every real output).
      * ``HPi``/``LPi`` — crossover-filter indices (physical outputs only; out-of-range
                   sentinel == section off).
      * ``EqBy=0``— EQ bypass off.

    MUTE is carried by ``<Vol L>`` (0 == muted), NOT by ``MT``. Delay is the ``<T>``
    element's ``T`` attribute in SAMPLES @48 kHz (``{PM,P,T}``), with a sibling ``<CHS>``
    carrying the delay mode.
    """
    attrib = {
        "ON": str(on), "CN": str(cn), "MT": "1", "CE": "1", "LG": "0",
        "Finit": str(finit), "CINV": str(int(cinv)), "DG": "0", "GD": "0", "EqBy": "0",
    }
    if hp_lp is not None:                        # physical output: crossover slots
        attrib["HPi"] = str(hp_lp[0])
        attrib["LPi"] = str(hp_lp[1])
    el = ET.Element("OC", attrib)
    for f in eq_els:
        el.append(f)
    # NEVER write Vol L=0: PC-Tool computes gain_dB = 20*log10(L) and 0 → -inf → CRASH.
    # A muted DSP cell reads back linear 0 (gain/mute share the OUTPUTMUTE cell), and .pct6
    # represents mute via a separate flag while keeping Vol L at the display gain — which we
    # can't recover from a muted cell — so fall back to unity (0 dB) rather than 0. (Mute is
    # therefore not round-tripped by from_device; documented limitation.)
    ET.SubElement(el, "Vol", {"L": f"{(abs(gain_lin) if gain_lin != 0 else 1.0):.6f}"})  # magnitude; sign=polarity(CINV)
    ET.SubElement(el, "T", {"PM": "1", "P": "0", "T": str(int(delay_samples))})
    ET.SubElement(el, "CHS", {"LATERAL": "0", "LONG": "0"})
    return el


def _ic_el(cn: int, gain_lin: float, eq_els: List["ET.Element"], *, in_index: int,
           finit: int) -> "ET.Element":
    """Build one ``<IC>`` input element matching the real M 5.4DSP schema.

    ``in_index`` sets the ``IN`` source-index (0..3 = analog Main A–D, 4/5 = Digital L/R).
    Analog inputs carry ``<Vol>``, ``<Fil>`` bands, a zero ``<T>`` and a ``<CHS>``; digital
    inputs (no tunable DSP params) are emitted flat: just a ``<CHS SH=3>`` marker, matching
    the vendor file. Real inputs carry ``MT=0`` (the assigned flag lives on outputs); their
    ``HPi``/``LPi`` point one past the band list (crossover off)."""
    n = len(eq_els)
    audio = n > 0
    attrib = {
        "IN": str(in_index), "CN": str(cn), "MT": "0", "Finit": str(finit),
        "CINV": "0", "HPi": str(n + 1), "LPi": str(n), "EqBy": "0",
    }
    el = ET.Element("IC", attrib)
    if audio:
        for f in eq_els:
            el.append(f)
        # Real inputs are always <Vol L=1> (unity) with MT=0 — inputs are never represented
        # as muted-via-Vol (unlike outputs). Fall back to unity for a non-positive read.
        ET.SubElement(el, "Vol", {"L": f"{(abs(gain_lin) if gain_lin != 0 else 1.0):.6f}"})  # magnitude; sign=polarity(CINV)
        ET.SubElement(el, "T", {"T": "0"})
        ET.SubElement(el, "CHS", {"LATERAL": "0", "LONG": "0"})
    else:                                        # digital source: flat, no audio params
        ET.SubElement(el, "CHS", {"LATERAL": "0", "SH": "3"})
    return el


def _total_input_count(model) -> int:
    """True number of PC-Tool input channels for *model* = analog line inputs + digital
    inputs. The digital inputs (Digital L/R) carry no tunable DSP params, so they do not
    appear in ``model.input_letters``; recover the full count from the input routing
    matrix's SOURCE columns (its ``sources``/``dims`` describe every input), falling back
    to just the analog count when no such matrix exists."""
    analog = len(model.input_letters)
    try:
        mtx = model.routing.input_to_virtual
        if mtx is not None:
            return max(analog, int(mtx.dims[1]))
    except Exception:
        pass
    return analog


def _from_device(device: "Device") -> "Setup":
    from atf_dsp import encoding
    from atf_dsp.models import dev_id_for_model

    model = device.model
    graphic = model._graphic_freqs()
    # <ATF Dev> is the PC-Tool INTERNAL device-type id (e.g. 29 for the M 5.4DSP), NOT the
    # USB PID. Only known-verified ids are emitted; unknown models fall back to 0 (never the
    # USB PID, which lives in a different number space — see atf_dsp.models.DEV_IDS).
    dev_id = dev_id_for_model(device.model_name)

    virtual_els: List[ET.Element] = []
    for vc in model.virtuals():
        gain = _read_gain_lin(device, vc.resolve_gain().name)
        n = vc.eq_band_count
        eq = _read_eq_bands_xml(device, vc.resolve_eq_band, min(n, len(graphic) or n), graphic)
        # ON = the virtual's slot index in the OUTS space (virtuals lead: 0..N-1).
        virtual_els.append(_oc_el(0, gain, eq, on=len(virtual_els), finit=len(eq)))

    n_virtual = len(virtual_els)
    output_els: List[ET.Element] = []
    for letter in model.output_letters:
        oc = model.output(letter)
        gain = _read_gain_lin(device, oc.resolve_gain().name)
        n = min(oc.eq_band_count, len(graphic) or oc.eq_band_count)
        eq = _read_eq_bands_xml(device, oc.resolve_eq_band, n, graphic)
        # delay: read DELAYAMT (integer samples @48k) -> keep raw sample count
        try:
            dd = device.read_param(oc.resolve_delay().name, nbytes=4)
            delay_raw = int.from_bytes(dd[:4], "big") if dd and len(dd) >= 4 else 0
        except Exception:
            delay_raw = 0
        # crossover: recover a best-effort corner + slope + characteristic from the FILTERS
        # stages (encoding.lphp_corner_from_coeffs). Append a highpass (<Fil T=10>) and/or lowpass
        # (<Fil T=9>) band for each ACTIVE section and point HPi/LPi at them; an off section is
        # left out-of-range ('off'). G carries the slope as -dB/oct (matches the PC-Tool sample
        # convention); Q carries the recovered corner-stage Q (its characteristic).
        try:
            xrec = oc.recover_crossover()
        except Exception:
            xrec = {}
        _XOVER_OFF = 999                             # sentinel: guaranteed out-of-range == off
        hp_idx = lp_idx = _XOVER_OFF
        finit = len(eq)                              # graphic bands only (before crossover)
        # The <Fil T> code encodes the crossover CHARACTERISTIC (confirmed vs the real M54 file +
        # PC-Tool): Butterworth HP=10/LP=9, Linkwitz-Riley HP=16/LP=15. (Bessel/other slopes TBD.)
        hp = xrec.get("highpass")
        if hp is not None and not hp.off and hp.corner_hz is not None:
            hp_idx = len(eq)
            eq.append(ET.Element("Fil", {
                "I": str(hp_idx), "T": ("16" if hp.characteristic == "linkwitz_riley" else "10"),
                "F": f"{hp.corner_hz:.2f}", "G": f"{-int(hp.slope_db)}", "Q": f"{hp.q0:.4f}",
                "FilBy": "0", "FilBBR": "0"}))
        lp = xrec.get("lowpass")
        if lp is not None and not lp.off and lp.corner_hz is not None:
            lp_idx = len(eq)
            eq.append(ET.Element("Fil", {
                "I": str(lp_idx), "T": ("15" if lp.characteristic == "linkwitz_riley" else "9"),
                "F": f"{lp.corner_hz:.2f}", "G": f"{-int(lp.slope_db)}", "Q": f"{lp.q0:.4f}",
                "FilBy": "0", "FilBBR": "0"}))
        # polarity: read the per-output phase-invert cell -> CINV (0 normal / 1 inverted).
        inverted = _read_polarity_inverted(device, oc)
        # ON = physical outputs follow the virtuals in the OUTS space (N.. where N = #virtuals).
        output_els.append(_oc_el(0, gain, eq, on=n_virtual + len(output_els), finit=finit,
                                 hp_lp=(hp_idx, lp_idx), delay_samples=delay_raw,
                                 cinv=1 if inverted else 0))

    input_els: List[ET.Element] = []
    in_index = 0
    for letter in model.input_letters:      # analog line inputs (Main A..D -> IN 0..3)
        ic = model.input(letter)
        try:
            gain = _read_gain_lin(device, ic.resolve_mute().name)
        except Exception:
            gain = 1.0
        eq = _read_eq_bands_xml(device, ic.resolve_eq_band, ic.eq_band_count, [])
        input_els.append(_ic_el(0, gain, eq, in_index=in_index, finit=len(eq)))
        in_index += 1
    # Digital inputs (Digital L/R -> IN 4/5 on the M 5.4DSP) have NO tunable DSP params in
    # the map, so they are absent from model.input_letters. Emit one flat <IC> per remaining
    # input source (no <Fil> bands, matching the real vendor file) so INS and the IN source
    # indices are correct. Unlike the analog Main inputs (whose SOURCE = user-assigned CN, not
    # recoverable -> left 0), the digital inputs have a FIXED hardware identity, so set their CN
    # explicitly: first digital = Digital In Left (38), second = Digital In Right (39). Otherwise
    # PC-Tool defaults BOTH unset digitals to "Digital In L", showing the R channel ambiguously.
    _DIGITAL_CN = (38, 39)                       # Digital In Left / Right (CN_NAMES)
    for d in range(max(0, _total_input_count(model) - len(input_els))):
        cn = _DIGITAL_CN[d] if d < len(_DIGITAL_CN) else 0
        input_els.append(_ic_el(cn, 1.0, [], in_index=in_index, finit=0))
        in_index += 1

    # routing: read all three matrices into <R> blocks (row-major G0.., n = row*INS + col).
    # PC-Tool splits the input side into a MAIN block (the 6-wide MAINMATRIX/input_to_virtual)
    # and a separate DIGITAL block (the 9x2 OPTOMIXER/digital_to_virtual, INS=2 OUTS=9), then the
    # VIRTUAL->OUTPUT block. Block order + dims + G-orientation confirmed against a real M54 file.
    route_el = ET.Element("Route")
    ridx = 0
    for name in ("input_to_virtual", "digital_to_virtual", "virtual_to_output"):
        mtx = getattr(model.routing, name, None)
        if mtx is None:
            continue
        rows, cols = mtx.dims
        attrib = {"INS": str(cols), "OUTS": str(rows)}
        n = 0
        for r in range(rows):
            for c in range(cols):
                pname = mtx.template.format(row=f"{r:02d}", col=f"{c:02d}")
                if pname in model.params:
                    data = device.read_param(pname, nbytes=4)
                    lin = encoding.from_fixed(int.from_bytes(data[:4], "big")) if data and len(data) >= 4 else 0.0
                else:
                    lin = 0.0
                attrib[f"G{n}"] = f"{lin:.6f}"
                n += 1
        route_el.append(ET.Element("R", attrib))
        ridx += 1

    root = ET.Element("ATF", {
        "Dev": str(dev_id if dev_id is not None else 0),
        "V": "6.04.01",
        "OUTS": str(len(virtual_els) + len(output_els)),
        "INS": str(len(input_els)),
        # TM = time-alignment mode (1 = delay/ms, 0 = distance/cm). We write delays as sample
        # counts in <T T=…>, so present them in delay mode; otherwise PC-Tool defaults to distance
        # mode and shows 0 cm (it reads <CHS>, which we leave at 0). Other setup-level global flags
        # (VCO/VM/IOR/AV/IGL/IGM/FV/ICT/JPT) + blocks (DCM/ABP/STX/MCV2/ATFCOND) are not DSP-RAM
        # params and are intentionally omitted; PC-Tool defaults them.
        "TM": "1",
        "FN": "from_device_export.pct6",
    })
    for el in (*virtual_els, *output_els, *input_els):
        root.append(el)
    root.append(route_el)
    return Setup.from_xml(ET.tostring(root, encoding="unicode").encode("utf-8"), variant="XOR:ATFV6")


# ---------------------------------------------------------------------------
# 3) applying a Setup to a Device (dry-run by default)
# ---------------------------------------------------------------------------
@dataclass
class ApplyResult:
    """Outcome of :func:`apply_setup`: what was written vs skipped (and why)."""

    executed: bool               # did we actually emit writes (vs plan-only)?
    writes: int                  # number of successful control writes emitted
    skipped: int                 # channel-level writes skipped (unresolved / gated)
    frames: int                  # number of link frames produced (dry-run capture)
    pid_setup: Optional[int]
    pid_device: Optional[int]


class PidMismatchError(RuntimeError):
    """Raised by :func:`apply_setup` when the setup's device id does not match the
    connected device and ``force`` was not set."""


def plan_setup_writes(device: "Device", setup: Setup):
    """Compute the (param_name, value_bytes) writes a Setup maps to, WITHOUT emitting them.

    The single source of truth for what an apply touches: gain/mute (confirmed encoders)
    and peaking EQ bands (confirmed). Delay/crossover/routing are gated encoders and are
    deliberately skipped here. Returns ``(writes, skipped)`` where *writes* is a list of
    ``(name, bytes)`` and *skipped* counts channel-level items that could not be resolved.
    """
    from atf_dsp import encoding
    from atf_dsp.channels import ChannelModelError

    model = device.model
    writes: List[Tuple[str, bytes]] = []
    skipped = 0

    def _plan_channel(get_channel, ch: Channel, band_limit: int) -> None:
        nonlocal skipped
        try:
            target = get_channel()
        except ChannelModelError:
            skipped += 1
            return
        # gain / mute (shared OUTPUTMUTE/VCP gain cell); inputs only carry a mute cell.
        try:
            if hasattr(target, "resolve_gain"):
                gref = target.resolve_gain()
                raw = 0 if ch.mute else encoding.gain_db_to_raw(ch.gain_db if ch.gain_db is not None else 0.0)
                writes.append((gref.name, encoding.to_bytes_be(raw)))
            elif ch.mute:
                writes.append((target.resolve_mute().name, encoding.to_bytes_be(0)))
        except (ChannelModelError, ValueError, AttributeError):
            skipped += 1
        # EQ bands (peaking only)
        for i, band in enumerate(ch.eq_bands):
            if i >= band_limit or band.bypass or band.kind != "peaking":
                continue
            try:
                ref = target.resolve_eq_band(i)
                coeffs = encoding.biquad_rbj("peaking", band.freq, band.q, band.gain_db, fs=device.fs)
                writes.append((ref.base, b"".join(encoding.to_bytes_be(c) for c in coeffs)))
            except (ChannelModelError, ValueError):
                skipped += 1

    try:
        virt_keys = [v.key for v in model.virtuals()]
    except Exception:
        virt_keys = []
    for ch in setup.virtuals:
        if ch.index >= len(virt_keys):
            skipped += 1
            continue
        key = virt_keys[ch.index]
        _plan_channel(lambda k=key: model.virtual(k), ch, model.virtual(key).eq_band_count)

    out_letters = model.output_letters
    for ch in setup.outputs:
        if ch.index >= len(out_letters):
            skipped += 1
            continue
        letter = out_letters[ch.index]
        _plan_channel(lambda l=letter: model.output(l), ch, model.output(letter).eq_band_count)

    in_letters = model.input_letters
    for ch in setup.inputs:
        if ch.index >= len(in_letters):
            # Inputs beyond the physical line inputs are DIGITAL sources (Digital L/R) with
            # no tunable DSP params — nothing to write. Only flag as skipped (a genuinely
            # lost mapping) if such a channel actually carries writable content.
            if ch.eq_bands or ch.mute:
                skipped += 1
            continue
        letter = in_letters[ch.index]
        _plan_channel(lambda l=letter: model.input(l), ch, model.input(letter).eq_band_count)

    return writes, skipped


def apply_setup(
    device: "Device",
    setup: Setup,
    dry_run: bool = True,
    force: bool = False,
) -> ApplyResult:
    """Map ``setup``'s per-channel values onto ``device``'s channel model and write them
    through the hardware-CONFIRMED typed API (gain + EQ; delay/crossover/routing skipped).

    Safety model:
      * ``dry_run=True`` (DEFAULT) never transmits to real hardware. If the device's link
        is a dry-run link the writes are still *built* (so tests can inspect the emitted
        ``0x03`` frames); if the link is a REAL port nothing is written — a plan-only result
        is returned.
      * ``dry_run=False`` is the LIVE apply (RE-2): it snapshots every affected param,
        writes them all, reads each back and verifies, and on ANY mismatch/error
        AUTO-RESTORES the snapshot and raises :class:`atf_dsp.device.ApplyError`. Fully
        exercised offline against the memory-backed dry-run link.
      * Device-match: the setup's INTERNAL device-type id ``setup.metadata.pid`` (``<ATF
        Dev>``) is checked against the connected device's OWN internal ``Dev`` id
        (``dev_id_for_model(device.model_name)``) — same number space, NOT the USB PID.
        A mismatch raises :class:`PidMismatchError` unless ``force``. When the device's
        internal id is unknown (not in :data:`atf_dsp.models.DEV_IDS`) the check degrades
        gracefully — it is skipped so a legitimate apply is never hard-failed.
    """
    from atf_dsp.models import dev_id_for_model

    # Compare like-for-like: both are PC-Tool INTERNAL Dev ids (e.g. 29 for the M 5.4DSP),
    # never the USB PID. dev_id_for_model returns None for models we haven't verified.
    dev_id_device = dev_id_for_model(device.model_name)
    pid_device = dev_id_device
    pid_setup = setup.metadata.pid
    if pid_setup is not None and dev_id_device is not None and pid_setup != dev_id_device and not force:
        raise PidMismatchError(
            f"setup device id (Dev={pid_setup}) != connected device id (Dev={dev_id_device}); "
            f"pass force=True to apply anyway (values may be meaningless across models)."
        )

    link = device.link
    # Execute when the link is a safe dry-run link, or when the caller opted into a real apply.
    execute = link.dry_run or (not dry_run)
    result = ApplyResult(
        executed=execute, writes=0, skipped=0, frames=0,
        pid_setup=pid_setup, pid_device=pid_device,
    )
    if not execute:
        return result

    writes, skipped = plan_setup_writes(device, setup)
    result.skipped = skipped
    frames_before = len(link.sent)

    if not dry_run:
        # LIVE: snapshot -> write-all -> readback-verify -> restore-on-fail.
        device.write_verified(writes, snapshot=True, verify=True)
    else:
        # Dry-run: emit each write (SafeLoad 0x03) so frames can be inspected; no verify.
        for name, value in writes:
            device.write_param(name, value, safeload=True)

    result.writes = len(writes)
    result.frames = len(link.sent) - frames_before
    return result
