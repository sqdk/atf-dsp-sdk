"""channels.py — the CHANNEL-MODEL abstraction layer over a Device + ParamMap.

This raises the raw ``MOD_..._ADDR`` parameter surface to the PC-Tool's mental
model: physical *inputs*, *virtual* (tuning) channels, physical *outputs*, and the
virtual->output *routing matrix*. It is DATA-DRIVEN and model-agnostic — every count
and shape comes from ``tools/discover_channels.discover`` (the STRUCTURAL topology of
one model's inflated ``.at01``) combined with a small ``<model>.channels.yaml``
SEMANTIC overlay (labels, EQ-block roles, virtual identities, routing orientation).
Nothing about a specific model is baked into the code here.

Everything the overlay marks ``provisional: true`` is an UNCONFIRMED best-guess
pending a PC-Tool screenshot; the engine READS those values so a one-line overlay
edit flips behaviour. Provisional mappings are surfaced via ``.provisional`` on the
resolved references and log a one-time warning when written through — reads are
always safe and never gated here.

DSP math (biquads, gain/delay encoding) and the glitch-free firmware-SafeLoad write
path are reused wholesale from :mod:`atf_dsp.encoding` and :mod:`atf_dsp.controls`
(which write via ``Device.write_param(safeload=True)``); this module only does
topology + name resolution.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

import yaml

from atf_dsp import encoding
from atf_dsp.controls import Controls, NotConfirmedError
from atf_dsp.encoding import EQ_STORAGE_ORDER
from atf_dsp.models import model_fs
from atf_dsp.params import ParamMap

# discover() is the single source of structural truth — never re-implement it here.
import sys as _sys

_TOOLS_DIR = Path(__file__).resolve().parent.parent / "tools"
if str(_TOOLS_DIR) not in _sys.path:
    _sys.path.insert(0, str(_TOOLS_DIR))
from discover_channels import discover  # noqa: E402

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).resolve().parent / "data"

# Built-in name templates (SigmaStudio export convention). The overlay's `templates:`
# block overrides any of these, so a model with different naming needs only overlay
# edits — these are the model-agnostic defaults, not a per-model hardcode.
_DEFAULT_TEMPLATES: Dict[str, str] = {
    "input_eq": "MOD_INPUT_EQ_LINE_INPUTEQ{letter}_ALG0_STAGE{band}_{coeff}",
    "eq1": "MOD_EQUALIZER_EQ1{inst}_ALG0_STAGE{band}_{coeff}",
    "eq2": "MOD_EQUALIZER_EQ2{inst}_ALG0_STAGE{band}_{coeff}",
    "output_gain": "MOD_OUTPUTMUTE_ALG0_MUTE{index}",   # gain/mute cell (hw-confirmed)
    "virtual_gain": "MOD_VCPMUTE_ALG0_MUTE{index}",      # VCP gain/mute cell (hw-confirmed)
    "output_delay": "MOD_DELAY__PHASE_SWITCH_DELAY{letter}_DELAYAMT",
    # LEGACY per-output INV{letter} cell in the DELAY__PHASE_SWITCH module. This is NOT the real
    # polarity: it reads 0 for both normal AND inverted. Actual polarity is the SIGN of the output
    # gain/mute cell (see OutputChannel.polarity / is_inverted / gain — hardware-confirmed
    # 2026-08-30). Kept only so the export field enumeration (validate.py) can name the cell; the
    # numeric suffix after INV{letter}_ is not a clean function of the letter, hence a prefix template.
    "output_polarity": "MOD_DELAY__PHASE_SWITCH_INV{letter}_*",
    "output_mute": "MOD_OUTPUTMUTE_ALG0_MUTE{index}",
    "input_mute": "MOD_INPUTGAIN_ALG0_MUTE{index}",
    "output_xover": "MOD_FILTERS__PHASE_12DB_{letter}_ALG0_STAGE{stage}_{coeff}",
    # Per-output WHOLE-EQ bypass — a mono-mux that routes around the EQUALIZER cascade (dry vs
    # EQ'd), NON-destructive (bands are preserved). The MONOMUX NS# suffix varies per output, so
    # this is a prefix-wildcard template. INDEX value hardware-confirmed 2026-08-31 (int32,
    # 1 = bypass / 0 = active).
    "output_eq_bypass": "MOD_EQUALIZER_BYPASS{letter}_*INDEX",
}

# Virtual channel -> its GLOBAL_EQ whole-EQ bypass mono-mux cell prefix (the region+side that its
# EQ bands already resolve into: `vc.resolve_eq_band(0)` -> MOD_GLOBAL_EQ_<REGION>_...). Same
# mono-mux encoding as the output bypass (int32 1=bypass/0=active). G is pass-through (no EQ).
_VIRTUAL_EQ_BYPASS_PREFIX = {
    "A": "MOD_GLOBAL_EQ_FRONT_BYPASSFL",
    "B": "MOD_GLOBAL_EQ_FRONT_BYPASSFR",
    "C": "MOD_GLOBAL_EQ_REAR_BYPASSFL",
    "D": "MOD_GLOBAL_EQ_REAR_BYPASSFR",
    "E": "MOD_GLOBAL_EQ_CENTER_BYPASSCF",
    "F": "MOD_GLOBAL_EQ_CENTER_REAR_BYPASSCR",
    "H": "MOD_GLOBAL_EQ_SUB_BYPASSSUBL",
    "I": "MOD_GLOBAL_EQ_SUB_BYPASSSUBR",
}
# Crossover FILTERS stage layout — CORRECTED 2026-08-30 against the PC-Tool (real oracle). Each
# output has 9 FILTERS stages; a section spans up to 3 cascaded biquads (12/24/36 dB = 1/2/3
# stages), filling ascending: 12 dB = first stage, 24 dB = first two, 36 dB = all three; the rest
# read as bypass. HP = [0,1,2], LP = [3,4,5] (stages 6-8 are likely the PHASE/allpass block).
#   ORACLE-CONFIRMED: LP stages 3 AND 4 hold a PC-Tool LR24 @80 (Output F, setup 7). The earlier
#   LP=[3,2] was WRONG — it read stage 2 (a HP-block slot, always bypass) as the LP's 2nd section,
#   so a real LR24 mis-recovered as BW12. It only ever "passed" because Direction B is circular
#   (same encoder both ends). INFERRED (pending a 36 dB oracle read): LP stage 5 and HP stages 1,2.
# Per-stage Q by characteristic (RBJ highpass/lowpass):
_XOVER_STAGES = {"highpass": [0, 1, 2], "lowpass": [3, 4, 5]}
# FILTERS stages 6-8 are the PHASE all-pass block (hardware-confirmed 2026-08-30: Output F's
# phase used STAGE8). A single 2nd-order all-pass covers 0..360 deg.
_PHASE_STAGES = [6, 7, 8]

# Whole-EQ bypass mono-mux INDEX values — HARDWARE-CONFIRMED 2026-08-31 (audible A/B on the amp):
# a plain int32 index, 1 = bypass (dry path), 0 = EQ active.
EQ_BYPASS_INDEX = 1
EQ_ACTIVE_INDEX = 0
_XOVER_Q = {
    # 12/24 dB hardware-confirmed 2026-08-26; 36 dB added 2026-08-30.
    # LR36 = oracle-captured off the amp (Output F, −36 dB LR). BW36 = the standard 6th-order
    # Butterworth section Qs (not yet oracle-captured — set a −36 dB BW to confirm).
    "butterworth":    {12: [0.7071], 24: [0.5412, 1.3066], 36: [0.5176, 0.7071, 1.9319]},
    "linkwitz_riley": {12: [0.5],    24: [0.7071, 0.7071], 36: [0.5701, 0.7400, 1.1698]},
}

# One-time-warning bookkeeping (keyed by a stable mapping id).
_WARNED: set = set()


def _infer_crossover_characteristic(slope: int, q0: float, *, tol: float = 0.05) -> Optional[str]:
    """Best-effort characteristic ('butterworth' | 'linkwitz_riley') from a recovered
    (slope, corner-stage Q). Matches ``q0`` against the first-stage Q of each entry in the
    confirmed ``_XOVER_Q`` table. With the SDK's own write order (stage[0]/stage[3] carries
    ``qs[0]``) the four supported combos have DISTINCT (slope, q0) pairs — butterworth-12=0.7071,
    LR-12=0.5, butterworth-24=0.5412, LR-24=0.7071 — so this is reliable for them; anything else
    returns None (documented best-guess, never fabricated). See ``recover_crossover``."""
    for char, table in _XOVER_Q.items():
        qs = table.get(slope)
        if qs and abs(qs[0] - q0) <= tol:
            return char
    return None


def crossover_characteristic(slope: int, q0: float, *, tol: float = 0.05) -> str:
    """Classify a crossover section from its ``(slope, first-stage Q)`` ->
    ``'butterworth' | 'linkwitz_riley' | 'custom'``. PURE — no Device — so a browser/bridge that
    read the stage {corner, Q} itself can classify per section. Correct for 12/24/36 dB; returns
    **'custom'** (not None) for a Q that matches no standard table entry (e.g. a self-defined
    filter at Q=1.5)."""
    return _infer_crossover_characteristic(slope, q0, tol=tol) or "custom"


@dataclass(frozen=True)
class CrossoverRecovery:
    """One recovered crossover section (HP or LP) read back off the DSP FILTERS stages.

    ``corner_hz``/``q0`` are the EXACT analytic inverse of the written RBJ pole coefficients
    (``encoding.lphp_corner_from_coeffs``); ``slope_db`` comes from the count of active (non-bypass)
    stages in the HP/LP pair (1 stage = 12 dB/oct, 2 = 24); ``characteristic`` is a best-effort
    inference from (slope, q0) against the confirmed ``_XOVER_Q`` table (may be None). ``active``
    is the raw active-stage count. ``off`` (no active stage) means the section is disabled.
    Marked ``approximate=True``: corner/slope are trustworthy, characteristic is inferred."""

    kind: str                          # 'highpass' | 'lowpass'
    corner_hz: Optional[float]
    slope_db: Optional[int]            # 12 | 24, or None when off
    q0: Optional[float]
    characteristic: Optional[str]
    active: int
    approximate: bool = True

    @property
    def off(self) -> bool:
        return self.active == 0


class ChannelModelError(RuntimeError):
    """Raised when a channel/control cannot be resolved or written."""


def _warn_provisional_once(key: str, detail: str) -> None:
    if key not in _WARNED:
        _WARNED.add(key)
        logger.warning("writing through PROVISIONAL mapping (%s): %s — confirm against PC-Tool UI", key, detail)


def _inst(n: int) -> str:
    """SigmaStudio instance suffix: '' for instance 1, else '_<n>'."""
    return "" if n == 1 else f"_{n}"


@dataclass(frozen=True)
class ParamRef:
    """A resolved scalar control: a single param name + address, with provisional flag."""

    name: str
    addr: int
    provisional: bool


@dataclass(frozen=True)
class EqBandRef:
    """A resolved EQ band: the 5 consecutive biquad coeff params (EQ_STORAGE_ORDER),
    the base (B2) name/address to SafeLoad-burst, plus its (block, stage) origin."""

    names: List[str]
    base: str
    base_addr: int
    block: str
    stage: int
    provisional: bool


# ---------------------------------------------------------------------------
# overlay loading
# ---------------------------------------------------------------------------
def _load_overlay(model_name: str) -> Optional[dict]:
    """Load ``<model_name>.channels.yaml`` from atf_dsp/data (None if absent)."""
    path = DATA_DIR / f"{model_name}.channels.yaml"
    if not path.exists():
        return None
    with path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


# ---------------------------------------------------------------------------
# channel objects
# ---------------------------------------------------------------------------
class _Channel:
    """Common behaviour for input/output/virtual channels."""

    def __init__(self, model: "ChannelModel", letter: Optional[str], label: str,
                 setup_label: Optional[str] = None) -> None:
        self._model = model
        self.letter = letter
        # `.label` is the CANONICAL, PC-Tool-resonant identity: class + letter, e.g. "Output A",
        # "Virtual H", "Input A" — matching the tool's [A]/[H] pointers. It is stable per device.
        self.label = label
        # `.setup_label` is an OPTIONAL, illustrative friendly name (e.g. "Front Left") that a
        # SETUP assigns. It is per-setup and per-device — never a hardware constant — so it is
        # never used as an addressing key. May be None.
        self.setup_label = setup_label

    # -- write plumbing ---------------------------------------------------
    def _controls(self) -> Controls:
        return self._model._controls()

    def _write_eq(self, ref: EqBandRef, f: float, Q: float, gain_db: float, kind: str) -> None:
        if ref.provisional:
            _warn_provisional_once(f"eq:{ref.base}", f"{self.label} band -> {ref.block} STAGE{ref.stage}")
        self._controls().set_eq_band(ref.base, f, Q, gain_db, kind=kind)

    def bypass_eq_band(self, i: int) -> "_Channel":
        """Bypass a SINGLE EQ band by writing the pass-through image ``[0,0,UNITY,0,0]`` to its
        stage — the band does nothing (flat). This DSP has no per-band bypass cell: a bypassed band
        IS the pass-through biquad (recovery / :func:`encoding.eq_band_from_coeffs` read it back as
        None). DESTRUCTIVE at the DSP level (loses the band's f/Q/gain) — re-call
        :meth:`eq_band` to restore, so keep the band's settings client-side (like mute/gain).
        Works on inputs, outputs and virtuals; dry-run buildable."""
        ref = self.resolve_eq_band(i)
        if ref.provisional:
            _warn_provisional_once(f"eq:{ref.base}", f"{self.label} band {i} -> bypass")
        ctrls = self._controls()
        ctrls._write_words(ref.base_addr, [0, 0, encoding.UNITY, 0, 0])
        return self


class _GraphicEqMixin:
    """Convenience for the 30-band graphic-EQ tiers (output + virtual): address a band by
    NUMBER at its canonical ISO centre frequency, so you can set just the gain without
    re-specifying frequency/Q. Full parametric control stays on ``eq_band(i, f, Q, ...)``."""

    def band_frequency(self, i: int) -> float:
        """The canonical ISO centre frequency (Hz) of graphic band *i* (from the overlay)."""
        freqs = self._model._graphic_freqs()
        if not freqs:
            raise ChannelModelError(
                f"{self.label}: no graphic ISO frequency table in the overlay (add "
                f"graphic_eq.frequencies_hz) — use eq_band(i, f=..., Q=..., gain_db=...) instead")
        if not (0 <= i < len(freqs)):
            raise ChannelModelError(
                f"{self.label}: band {i} has no graphic ISO frequency (valid 0..{len(freqs) - 1}) "
                f"— use eq_band(i, f=..., Q=..., gain_db=...) for a custom/parametric band")
        return float(freqs[i])

    def set_band_gain(self, i: int, gain_db: float, Q: Optional[float] = None,
                      kind: str = "peaking"):
        """Set ONLY the gain of graphic band *i* (by number). Frequency and Q stay at the
        canonical graphic values (ISO centre + default graphic Q, both from the overlay), so a
        graphic-EQ "move slider N to X dB" is a one-liner. Recomputes the band's biquad at
        (ISO f, Q, gain_db) and returns the channel (chainable).

        NOTE: this treats band *i* as a graphic band; it does NOT read back and preserve a
        previously-*customised* frequency/Q (that requires a hardware read-back). For custom
        f/Q, use eq_band() directly."""
        q = self._model._graphic_q() if Q is None else Q
        return self.eq_band(i, f=self.band_frequency(i), Q=q, gain_db=gain_db, kind=kind)


class InputChannel(_Channel):
    """A physical line input. Input EQ and input mute are hardware-CONFIRMED."""

    provisional = False

    def __init__(self, model: "ChannelModel", letter: str, setup_label: Optional[str] = None) -> None:
        super().__init__(model, letter, f"Input {letter}", setup_label)

    @property
    def eq_band_count(self) -> int:
        return self._model.topology["inputs"]["eq_bands"]

    def resolve_eq_band(self, i: int) -> EqBandRef:
        """Resolve input-EQ band *i* to its 5 INPUTEQ<letter> STAGE<i> coeff params."""
        return self._model._resolve_input_eq(self.letter, i)

    def resolve_mute(self) -> ParamRef:
        return self._model._resolve_input_mute(self.letter)

    def eq_band(self, i: int, f: float, Q: float, gain_db: float, kind: str = "peaking") -> "InputChannel":
        self._write_eq(self.resolve_eq_band(i), f, Q, gain_db, kind)
        return self

    def mute(self, muted: bool = True) -> "InputChannel":
        ref = self.resolve_mute()
        self._controls().set_mute(ref.name, muted)
        return self


class OutputChannel(_GraphicEqMixin, _Channel):
    """A physical amplifier output. Gain / mute / delay / crossover BY LETTER are
    hardware-CONFIRMED (non-provisional). The per-output EQ spans two cascaded blocks
    (EQ1[15] + EQ2[16]); the block ORDER is order-independent by construction (parametric
    bands + a commuting cascade — see ``_band_to_block_stage``), so it needs no oracle."""

    provisional = False

    def __init__(self, model: "ChannelModel", letter: str, index: int,
                 setup_label: Optional[str] = None) -> None:
        super().__init__(model, letter, f"Output {letter}", setup_label)
        self.index = index  # 0-based physical position (A=0)

    @property
    def eq_band_count(self) -> int:
        return self._model._class_band_count("output")

    @property
    def eq_provisional(self) -> bool:
        return self._model._class_eq_provisional("output")

    # -- resolution -------------------------------------------------------
    def resolve_gain(self) -> ParamRef:
        return self._model._resolve_output_gain(self.index)

    def resolve_mute(self) -> ParamRef:
        return self._model._resolve_output_mute(self.index)

    def resolve_delay(self) -> ParamRef:
        return self._model._resolve_output_delay(self.letter)

    def resolve_polarity(self) -> ParamRef:
        """LEGACY — resolves the ``INV{letter}`` cell in the DELAY__PHASE_SWITCH module. NOTE:
        that cell is NOT the real polarity (it reads 0 for both normal AND inverted). The actual
        polarity is the SIGN of the output gain/mute cell (see :meth:`polarity` / :meth:`is_inverted`
        / :meth:`gain`, hardware-confirmed 2026-08-30). Kept only for the export field enumeration."""
        return self._model._resolve_output_polarity(self.letter)

    def resolve_eq_band(self, i: int) -> EqBandRef:
        return self._model._resolve_class_eq("output", self.index + 1, i)

    def resolve_crossover(self, kind: str, stage_pos: int = 0) -> EqBandRef:
        """Resolve one crossover biquad stage. kind in {highpass,lowpass}; stage_pos is the
        position within the section's 3-stage block (0 = 12 dB; 0,1 = 24 dB; 0,1,2 = 36 dB)."""
        stage = _XOVER_STAGES[kind][stage_pos]
        return self._model._resolve_output_xover(self.letter, stage)

    # -- gain / polarity (one shared cell: value = sign × magnitude, 0 = mute) ---
    def _gain_cell_signed(self) -> Optional[int]:
        dev = self._model.device
        if dev is None:
            return None
        data = dev.read_param(self.resolve_gain().name, nbytes=4)
        if not data or len(data) < 4:
            return None
        return int.from_bytes(data[:4], "big", signed=True)

    def gain_db(self) -> Optional[float]:
        """Current gain MAGNITUDE in dB (polarity-independent). ``None`` if muted (cell == 0)."""
        raw = self._gain_cell_signed()
        if raw is None:
            return None
        lin = abs(encoding.from_fixed(raw))
        return None if lin <= 0 else 20.0 * math.log10(lin)

    def is_inverted(self) -> bool:
        """True when polarity is inverted (the gain cell is negative)."""
        raw = self._gain_cell_signed()
        return raw is not None and raw < 0

    def _write_gain_cell(self, db: float, inverted: bool) -> None:
        mag = encoding.gain_db_to_raw(db)          # positive 8.24 magnitude
        raw = -mag if inverted else mag            # polarity = sign
        ctrls = self._controls()
        ctrls._write_word(ctrls._addr(self.resolve_gain().name), raw)

    def gain(self, db: float, inverted: bool = False) -> "OutputChannel":
        """Set gain magnitude (dB) and polarity in ONE write (they share the cell:
        value = sign × magnitude). ``inverted`` defaults to normal; pass ``inverted=True`` (or use
        :meth:`polarity`) to invert. NOTE: a bare ``gain(db)`` sets NORMAL polarity — to change
        gain while keeping an existing inversion, pass ``inverted=self.is_inverted()``."""
        self._write_gain_cell(db, inverted)
        return self

    def polarity(self, inverted: bool = True) -> "OutputChannel":
        """Set output polarity (Helix "Polarity normal/inverted") by flipping the SIGN of the
        gain cell, preserving the gain magnitude. Hardware-confirmed encoding (2026-08-30)."""
        db = self.gain_db()
        self._write_gain_cell(0.0 if db is None else db, inverted)
        return self

    def mute(self, muted: bool = True) -> "OutputChannel":
        # Muting writes 0 to the shared cell (loses gain+polarity); unmute restores 0 dB normal.
        # To preserve gain/polarity across a mute, snapshot the raw cell and restore it.
        self._controls().set_mute(self.resolve_mute().name, muted)
        return self

    # -- whole-EQ bypass (mono-mux; NON-destructive — bands are preserved) ---
    def resolve_eq_bypass(self) -> ParamRef:
        """Resolve this output's whole-EQ bypass mono-mux cell (routes around the EQUALIZER
        cascade)."""
        return self._model._resolve_output_eq_bypass(self.letter)

    def eq_bypass_raw(self) -> Optional[int]:
        """The raw mono-mux INDEX currently in the EQ-bypass cell (or None with no Device)."""
        dev = self._model.device
        if dev is None:
            return None
        data = dev.read_param(self.resolve_eq_bypass().name, nbytes=4)
        return None if not data or len(data) < 4 else int.from_bytes(data[:4], "big")

    def is_eq_bypassed(self) -> Optional[bool]:
        """True when the whole output EQ block is bypassed (mux == 1). ``None`` with no Device."""
        raw = self.eq_bypass_raw()
        return None if raw is None else (raw == EQ_BYPASS_INDEX)

    def bypass_eq(self, bypass: bool = True) -> "OutputChannel":
        """Bypass / re-enable the WHOLE output EQ via its mono-mux — NON-destructive (the band
        coeffs are untouched, unlike :meth:`bypass_eq_band`). Hardware-confirmed 2026-08-31
        (int32 index, 1 = bypass / 0 = active)."""
        idx = EQ_BYPASS_INDEX if bypass else EQ_ACTIVE_INDEX
        self._controls()._write_word(self.resolve_eq_bypass().addr, idx)
        return self

    # -- phase (all-pass in FILTERS stages 6-8; -theta deg at the crossover freq) ---
    def resolve_phase(self, stage: int = _PHASE_STAGES[-1]) -> EqBandRef:
        """Resolve a phase all-pass FILTERS stage (6-8). Default = stage 8 (a single all-pass
        covers 0..360 deg; the PC-Tool used stage 8 for Output F)."""
        return self._model._resolve_output_xover(self.letter, stage)

    def phase_deg(self, ref_hz: Optional[float] = None) -> Optional[float]:
        """Recovered phase in DEGREES (positive lag, PC-Tool convention) — the all-pass phase at
        the crossover frequency. Scans stages 6-8 for the active all-pass; ``ref_hz`` overrides
        the crossover reference (auto-detected from the active LP/HP corner otherwise)."""
        dev = self._model.device
        if dev is None:
            return None
        ref = ref_hz if ref_hz is not None else self._crossover_ref_hz()
        if ref is None:
            return None
        for st in _PHASE_STAGES:
            data = dev.read_param(self._model._resolve_output_xover(self.letter, st).name, nbytes=20)
            words = [int.from_bytes(data[j:j + 4], "big") for j in range(0, min(len(data), 20), 4)]
            if len(words) == 5 and encoding.is_allpass(words):
                return -encoding.allpass_phase_deg(words, ref, fs=self._model.fs)  # positive magnitude
        return 0.0

    def phase(self, degrees: float, ref_hz: Optional[float] = None,
              stage: int = _PHASE_STAGES[-1]) -> "OutputChannel":
        """Set the phase all-pass to add ``degrees`` of phase at the crossover frequency
        (``ref_hz`` overrides the auto-detected crossover corner). Writes stage 8 by default."""
        ref = ref_hz if ref_hz is not None else self._crossover_ref_hz()
        if ref is None:
            raise ChannelModelError("phase() needs a crossover reference frequency (set a "
                                    "crossover first, or pass ref_hz=)")
        coeffs = encoding.allpass_biquad(degrees, ref, fs=self._model.fs)
        ctrls = self._controls()
        ctrls._write_words(self.resolve_phase(stage).base_addr, coeffs)
        return self

    def _crossover_ref_hz(self) -> Optional[float]:
        """The crossover frequency the phase references: the active LP corner, else the HP."""
        rc = self.recover_crossover()
        if rc["lowpass"].corner_hz is not None:
            return rc["lowpass"].corner_hz
        if rc["highpass"].corner_hz is not None:
            return rc["highpass"].corner_hz
        return None

    def delay_ms(self, ms: float, unsafe: bool = False) -> "OutputChannel":
        # Delay is hardware-confirmed (integer samples) -> not gated. `unsafe` kept for API
        # symmetry (ignored while status is 'confirmed'). set_delay uses the device's fs, so the
        # sample count is correct for 96/192 kHz firmware, not just 48 kHz.
        self._controls().set_delay(self.resolve_delay().name, ms, unsafe=unsafe)
        return self

    def eq_band(self, i: int, f: float, Q: float, gain_db: float, kind: str = "peaking") -> "OutputChannel":
        self._write_eq(self.resolve_eq_band(i), f, Q, gain_db, kind)
        return self

    def crossover_section(self, kind: str, freq: Optional[float] = None,
                          characteristic: str = "butterworth", slope: int = 12,
                          q: Optional[List[float]] = None, unsafe: bool = False) -> "OutputChannel":
        """Set ONE crossover section INDEPENDENTLY. ``kind`` is 'highpass' | 'lowpass'.

        - ``freq=None`` turns the section OFF (all its stages idempotently bypassed).
        - ``characteristic`` in {'butterworth', 'linkwitz_riley', 'custom'}; ``slope`` in
          {12, 24, 36} (= 1/2/3 cascaded biquad stages).
        - ``characteristic='custom'`` takes an explicit per-stage ``q`` list (len == slope//12), so
          e.g. a single-stage self-defined HP at Q=1.5 is ``('highpass', 100, 'custom', 12, [1.5])``.

        EVERY stage of the section's 3-stage block is written (active stages get the RBJ filter, the
        rest the pass-through bypass image), so switching slopes never leaves a stale stage.
        Dry-run buildable — resolves param names + writes frames without a Device."""
        if kind not in _XOVER_STAGES:
            raise ChannelModelError(f"crossover section kind must be 'highpass' | 'lowpass', got {kind!r}")
        stages = _XOVER_STAGES[kind]
        ctrls = self._controls()
        if freq is None:                                     # OFF: bypass the whole section
            for pos in range(len(stages)):
                ctrls.bypass_crossover(self.resolve_crossover(kind, pos).base, unsafe=unsafe)
            return self
        char = characteristic.lower().replace("-", "_").replace(" ", "_")
        if char == "custom":
            qs = list(q or [])
            if slope not in (12, 24, 36) or len(qs) != slope // 12:
                raise ChannelModelError(
                    f"custom {slope} dB needs an explicit q list of {slope // 12} value(s); got {len(qs)}")
        else:
            if char not in _XOVER_Q or slope not in _XOVER_Q[char]:
                raise ChannelModelError(
                    f"crossover {characteristic} {slope} dB is not in the confirmed table "
                    f"(butterworth|linkwitz_riley x 12|24|36 dB) — or use characteristic='custom' "
                    f"with q=[...]")
            qs = _XOVER_Q[char][slope]
        if len(qs) > len(stages):
            raise ChannelModelError(f"{kind} has {len(stages)} stages; cannot write {len(qs)} sections")
        for pos in range(len(stages)):
            base = self.resolve_crossover(kind, pos).base
            if pos < len(qs):
                ctrls.set_crossover(base, freq, q=qs[pos], kind=kind, unsafe=unsafe)
            else:
                ctrls.bypass_crossover(base, unsafe=unsafe)
        return self

    def crossover(self, hp: Optional[float] = None, lp: Optional[float] = None,
                  characteristic: str = "butterworth", slope: int = 12,
                  unsafe: bool = False) -> "OutputChannel":
        """Set BOTH crossover sections with the SAME characteristic + slope (convenience over
        :meth:`crossover_section`). ``hp``/``lp`` corners (Hz, or None = that section OFF). For
        per-section characteristic/slope or custom Q, call :meth:`crossover_section` per section.

        Each `None` section is explicitly bypassed and every stage of the 3-stage block is written,
        so the result is idempotent and never leaves a stale filter (hw-validated 2026-08-28/30)."""
        self.crossover_section("highpass", hp, characteristic, slope, unsafe=unsafe)
        self.crossover_section("lowpass", lp, characteristic, slope, unsafe=unsafe)
        return self

    def recover_crossover(self, fs: Optional[int] = None) -> Dict[str, "CrossoverRecovery"]:
        """Read this output's crossover back off the DSP FILTERS stages and RECOVER, per
        section, a best-effort corner frequency + slope + characteristic.

        Reads the HP stages [0,1,2] and LP stages [3,4,5] (0x02 reads only — never writes),
        decodes each stage's 5 biquad coeffs, and inverts the pole coefficients via
        :func:`encoding.lphp_corner_from_coeffs`. The corner frequency is the (exact) inverse of
        the written coefficients; the slope is inferred from the number of active (non-bypass)
        stages (1 = 12 dB/oct, 2 = 24); the characteristic is a best-effort inference from the
        corner-stage Q against the confirmed ``_XOVER_Q`` table (see
        :class:`CrossoverRecovery` — ``approximate=True``). A section with no active stage is
        reported ``off``.

        Returns ``{'highpass': CrossoverRecovery, 'lowpass': CrossoverRecovery}``.
        """
        from atf_dsp import encoding

        dev = self._model.device
        if dev is None:
            raise ChannelModelError("recover_crossover needs a Device")
        if fs is None:
            fs = self._model.fs
        out: Dict[str, CrossoverRecovery] = {}
        for kind in ("highpass", "lowpass"):
            actives: List[tuple] = []
            for pos in range(len(_XOVER_STAGES[kind])):
                ref = self.resolve_crossover(kind, pos)
                data = dev.read_param(ref.base, nbytes=20)
                words = [int.from_bytes(data[j:j + 4], "big") for j in range(0, min(len(data), 20), 4)]
                rec = encoding.lphp_corner_from_coeffs(words, fs=fs) if len(words) == 5 else None
                if rec is not None:
                    actives.append(rec)
            if not actives:
                out[kind] = CrossoverRecovery(kind, None, None, None, None, 0)
                continue
            corner, q0 = actives[0]
            slope = 12 * len(actives)
            out[kind] = CrossoverRecovery(
                kind, corner, slope, q0,
                _infer_crossover_characteristic(slope, q0), len(actives))
        return out


class VirtualChannel(_GraphicEqMixin, _Channel):
    """A virtual (tuning) channel from the PC-Tool 'Virtual' tab. Its 30-band EQ maps
    onto two consecutive 15-stage ``MOD_GLOBAL_EQ`` sub-blocks (bands 0..14 -> first
    sub-block, 15..29 -> second), read from the overlay's per-channel ``eq_subblocks``.

    Virtual EQ addressing/module + channel identities are CONFIRMED (screenshot), so EQ
    resolution is non-provisional. Two sub-points remain unconfirmed and are documented in
    the overlay: (a) which sub-block is the low-frequency half (band ordering within the
    30-band span), and (b) the virtual gain/mute param mapping — hence ``gain``/``mute``
    raise ``ChannelModelError`` rather than guessing a target and writing silently."""

    # EQ addressing + channel identity are confirmed; band-ordering caveat is documented above.
    provisional = False

    def __init__(self, model: "ChannelModel", key: str,
                 eq_subblocks: List[str], index: int, pass_through: bool = False,
                 setup_label: Optional[str] = None) -> None:
        super().__init__(model, key, f"Virtual {key}", setup_label)
        self.key = key            # overlay letter key (A..I incl. 'G' = pass-through, no EQ)
        self.role = setup_label or key   # backward-compat alias (older callers used .role)
        self.index = index        # 0-based position in the overlay's channel table
        self.eq_subblocks = list(eq_subblocks)
        # A pass-through channel (e.g. Virtual G) carries no EQ: overlay marks pass_through
        # (or simply omits eq_subblocks). Its EQ accessors raise a clear error.
        self.pass_through = bool(pass_through) or not self.eq_subblocks

    def _no_eq_error(self) -> "ChannelModelError":
        return ChannelModelError(f"pass-through channel '{self.key}' has no EQ")

    @property
    def eq_band_count(self) -> int:
        """Total EQ bands = sum of stages across this channel's ordered sub-blocks
        (0 for a pass-through channel)."""
        if self.pass_through:
            return 0
        return sum(self._model._global_eq_stage_count(sb) for sb in self.eq_subblocks)

    def resolve_eq_band(self, i: int) -> EqBandRef:
        """Resolve band *i* to (sub-block, stage) by walking the ordered ``eq_subblocks``."""
        if self.pass_through:
            raise self._no_eq_error()
        return self._model._resolve_virtual_eq(self, i)

    def eq_band(self, i: int, f: float, Q: float, gain_db: float, kind: str = "peaking") -> "VirtualChannel":
        if self.pass_through:
            raise self._no_eq_error()
        self._write_eq(self.resolve_eq_band(i), f, Q, gain_db, kind)
        return self

    def resolve_gain(self) -> ParamRef:
        return self._model._resolve_virtual_gain(self.index)

    def resolve_mute(self) -> ParamRef:
        return self._model._resolve_virtual_gain(self.index)   # same VCP gain/mute cell

    def gain(self, db: float) -> "VirtualChannel":
        # Virtual gain/level = VCP gain/mute cell (hw-confirmed: MOD_VCPMUTE_ALG0_MUTE{index}).
        self._controls().set_gain(self.resolve_gain().name, db)
        return self

    def mute(self, muted: bool = True) -> "VirtualChannel":
        # Same VCP cell as gain: mute -> 0; unmute -> 0 dB (call gain() to set a level).
        self._controls().set_mute(self.resolve_mute().name, muted)
        return self

    # -- whole-EQ (GLOBAL_EQ) bypass mono-mux (NON-destructive; same encoding as outputs) ---
    def resolve_eq_bypass(self) -> ParamRef:
        """Resolve this virtual's GLOBAL_EQ bypass mono-mux (raises for a pass-through virtual)."""
        return self._model._resolve_virtual_eq_bypass(self.key)

    def eq_bypass_raw(self) -> Optional[int]:
        dev = self._model.device
        if dev is None:
            return None
        data = dev.read_param(self.resolve_eq_bypass().name, nbytes=4)
        return None if not data or len(data) < 4 else int.from_bytes(data[:4], "big")

    def is_eq_bypassed(self) -> Optional[bool]:
        """True when this virtual's whole GLOBAL_EQ is bypassed (mux == 1). None with no Device."""
        raw = self.eq_bypass_raw()
        return None if raw is None else (raw == EQ_BYPASS_INDEX)

    def bypass_eq(self, bypass: bool = True) -> "VirtualChannel":
        """Bypass / re-enable this virtual's WHOLE EQ via its GLOBAL_EQ mono-mux — NON-destructive.
        Hardware-confirmed 2026-08-31 (int32 index, 1 = bypass / 0 = active)."""
        idx = EQ_BYPASS_INDEX if bypass else EQ_ACTIVE_INDEX
        self._controls()._write_word(self.resolve_eq_bypass().addr, idx)
        return self


# ---------------------------------------------------------------------------
# routing matrices (there are TWO in the MATCH signal chain)
# ---------------------------------------------------------------------------
@dataclass
class RoutingMatrix:
    """ONE routing matrix (a NxN mix of 8.24 linear-gain coefficients).

    The MATCH signal chain has two of these, both exposed via ``ChannelModel.routing``:
    ``input_to_virtual`` ("Main/Digital to Virtual Routing", 9 virtual rows x 6 input
    cols, CONFIRMED) and ``virtual_to_output`` ("Virtual to Output Routing", 9 output
    rows x 9 virtual cols, orientation by convention — not yet screenshot-verified).

    Axis meaning (``rows_kind`` / ``cols_kind`` in {'virtual','input','output'}), dims,
    the ``confirmed`` flag and any source ``sources`` labels all come from the overlay's
    ``routing.matrices`` block — nothing is hardcoded. ``VOL{row}{col}`` uses two
    zero-padded digits each, with row = DESTINATION and col = SOURCE.

    Reads (0x02) are always safe. A write opts into the hypothesis routing encoder and,
    when the matrix orientation is not ``confirmed``, requires ``unsafe=True`` (and logs a
    one-time provisional warning)."""

    model: "ChannelModel"
    name: str
    template: str
    rows_kind: str
    cols_kind: str
    dims: tuple
    confirmed: bool = False
    sources: Optional[List[str]] = None
    _grid: Optional[List[List[Optional[float]]]] = field(default=None, repr=False)

    # -- backward-compat scalar accessors --------------------------------
    @property
    def rows(self) -> int:
        return self.dims[0]

    @property
    def cols(self) -> int:
        return self.dims[1]

    @property
    def provisional(self) -> bool:
        """True when this matrix's orientation is not yet screenshot-confirmed."""
        return not self.confirmed

    # -- axis resolution --------------------------------------------------
    def _axis_index(self, kind: str, selector, size: int) -> int:
        """Resolve a row/col selector (int index OR channel letter/label) to a 0-based
        index on the given axis, per that axis' ``kind``."""
        if isinstance(selector, bool):  # guard: bool is an int subclass
            raise ChannelModelError(f"invalid selector {selector!r}")
        if isinstance(selector, int):
            idx = selector
        elif kind == "input":
            idx = self.model._input_source_index(selector, self.sources)
        elif kind == "virtual":
            idx = self.model._virtual_axis_index(selector)
        elif kind == "output":
            idx = self.model._output_index(str(selector))
        else:
            raise ChannelModelError(f"unknown axis kind '{kind}'")
        if not 0 <= idx < size:
            raise ChannelModelError(
                f"{self.name} {kind} selector {selector!r} -> index {idx} out of range (0..{size - 1})")
        return idx

    def cell_param(self, row_sel, col_sel) -> str:
        """Resolve the VOL cell param for (row = destination, col = source).

        ``row_sel``/``col_sel`` accept an int index OR a channel letter/label mapped to
        the correct axis order (per ``rows_kind``/``cols_kind``)."""
        r = self._axis_index(self.rows_kind, row_sel, self.dims[0])
        c = self._axis_index(self.cols_kind, col_sel, self.dims[1])
        return self.template.format(row=f"{r:02d}", col=f"{c:02d}")

    def read(self, device=None) -> "RoutingMatrix":
        """Load the live matrix by reading every cell (0x02) via *device*."""
        from atf_dsp import encoding

        dev = device if device is not None else self.model.device
        if dev is None:
            raise ChannelModelError("RoutingMatrix.read needs a Device")
        grid: List[List[Optional[float]]] = []
        for r in range(self.dims[0]):
            row_vals: List[Optional[float]] = []
            for c in range(self.dims[1]):
                name = self.template.format(row=f"{r:02d}", col=f"{c:02d}")
                if name not in self.model.params:
                    row_vals.append(None)
                    continue
                data = dev.read_param(name, nbytes=4)
                row_vals.append(encoding.from_fixed(int.from_bytes(data, "big")) if len(data) >= 4 else None)
            grid.append(row_vals)
        self._grid = grid
        return self

    def set(self, row_sel, col_sel, gain_db: float = 0.0, unsafe: bool = False) -> "RoutingMatrix":
        """Set the (row = destination, col = source) mix-cell gain (dB -> 8.24 linear).

        Writes go through the hypothesis routing encoder (:meth:`Controls.set_routing`).
        When this matrix's overlay ``confirmed`` flag is false (unverified orientation) the
        write additionally requires ``unsafe=True`` and logs a one-time provisional
        warning; the encoder gate itself always requires ``unsafe=True`` on top of that."""
        name = self.cell_param(row_sel, col_sel)
        if not self.confirmed:
            _warn_provisional_once(
                f"route:{self.name}:{name}",
                f"{self.name} orientation UNVERIFIED (rows={self.rows_kind}, cols={self.cols_kind})")
            if not unsafe:
                raise NotConfirmedError(
                    f"routing matrix '{self.name}' orientation is not confirmed "
                    f"(rows={self.rows_kind}, cols={self.cols_kind}); pass unsafe=True to write anyway.")
        self.model._controls().set_routing(name, gain_db, unsafe=unsafe)
        return self

    def as_grid(self) -> List[List[Optional[float]]]:
        """Return the last-read matrix as rows x cols of gains in dB-linear (or None)."""
        if self._grid is None:
            raise ChannelModelError("call read(device) before as_grid()")
        return self._grid


class Routing:
    """Container for a model's routing matrices, exposing each by name plus a small
    backward-compatible surface that delegates to ``virtual_to_output``.

    Models whose overlay lacks ``routing.matrices`` get an empty container (both matrix
    attributes ``None``) rather than a crash."""

    def __init__(self, model: "ChannelModel", matrices: Dict[str, RoutingMatrix]) -> None:
        self._model = model
        self._matrices = dict(matrices)
        self.input_to_virtual: Optional[RoutingMatrix] = matrices.get("input_to_virtual")
        self.digital_to_virtual: Optional[RoutingMatrix] = matrices.get("digital_to_virtual")
        self.virtual_to_output: Optional[RoutingMatrix] = matrices.get("virtual_to_output")

    def matrix(self, name: str) -> RoutingMatrix:
        m = self._matrices.get(name)
        if m is None:
            raise ChannelModelError(f"no routing matrix '{name}' (have {list(self._matrices)})")
        return m

    def __bool__(self) -> bool:
        return bool(self._matrices)

    # -- backward-compat delegation to the virtual->output matrix ---------
    def _primary(self) -> RoutingMatrix:
        m = self.virtual_to_output or next(iter(self._matrices.values()), None)
        if m is None:
            raise ChannelModelError("this model has no routing matrices")
        return m

    @property
    def rows(self) -> int:
        return self._primary().dims[0]

    @property
    def cols(self) -> int:
        return self._primary().dims[1]

    @property
    def provisional(self) -> bool:
        return self._primary().provisional

    def read(self, device=None) -> RoutingMatrix:
        """Deprecated shim: read the virtual->output matrix (use ``.virtual_to_output``)."""
        return self._primary().read(device)

    def as_grid(self) -> List[List[Optional[float]]]:
        """Deprecated shim: grid of the virtual->output matrix."""
        return self._primary().as_grid()

    def cell_param(self, output, virtual) -> str:
        """Deprecated shim: virtual->output cell (row=output, col=virtual)."""
        return self._primary().cell_param(output, virtual)

    def route(self, virtual, output, gain_db: float = 0.0, unsafe: bool = False) -> RoutingMatrix:
        """Deprecated shim: set a virtual->output cell (use ``.virtual_to_output.set``)."""
        return self._primary().set(output, virtual, gain_db, unsafe=unsafe)


# ---------------------------------------------------------------------------
# the model
# ---------------------------------------------------------------------------
class ChannelModel:
    """Structural topology (discovered) + semantic overlay, exposed as channels."""

    def __init__(self, model_name: str, param_map: ParamMap, topology: dict,
                 overlay: Optional[dict], device=None) -> None:
        self.model_name = model_name
        self.params = param_map
        self.topology = topology
        self.overlay = overlay or {}
        self.device = device
        self._templates = dict(_DEFAULT_TEMPLATES)
        self._templates.update(self.overlay.get("templates", {}) or {})
        self._eq_class_map = self._build_eq_class_map()
        self._routing: Optional[RoutingMatrix] = None

    # -- construction -----------------------------------------------------
    @classmethod
    def load(cls, model_name: str, param_map: ParamMap, device=None) -> "ChannelModel":
        """Build the channel model for *model_name* from a ParamMap.

        Combines the discovered structural topology with the ``<model>.channels.yaml``
        semantic overlay. When the overlay is absent the physical layer is still built
        from discovery with generic labels.
        """
        topology = discover(param_map.to_dict())
        overlay = _load_overlay(model_name)
        return cls(model_name, param_map, topology, overlay, device=device)

    def _controls(self) -> Controls:
        if self.device is None:
            raise ChannelModelError("this operation needs a Device (pass device= to ChannelModel.load)")
        return Controls(self.device)

    @property
    def fs(self) -> int:
        """DSP processing rate (Hz) used by the fluent encoders — the device's rate when
        connected, otherwise derived from the model's .at01 basename (48 kHz default). This is
        what makes EQ/crossover/delay/phase math track 96/192 kHz firmware."""
        if self.device is not None:
            return self.device.fs
        return model_fs(self.model_name)

    # -- topology accessors ----------------------------------------------
    @property
    def input_letters(self) -> List[str]:
        return list(self.topology["inputs"]["letters"])

    @property
    def output_letters(self) -> List[str]:
        return list(self.topology["outputs"]["letters"])

    @property
    def eq_blocks(self) -> dict:
        return dict(self.topology.get("eq_blocks", {}))

    # -- graphic-EQ defaults (shared by output + virtual 30-band EQs) ------
    def _graphic_freqs(self) -> List[float]:
        """ISO centre frequencies for the graphic-EQ bands (from the overlay)."""
        ge = self.overlay.get("graphic_eq", {}) or {}
        return list(ge.get("frequencies_hz") or self.overlay.get("output_eq_frequencies_hz") or [])

    def _graphic_q(self) -> float:
        """Default Q for a graphic-EQ band set by number (overlay, else 1/3-oct constant-Q)."""
        ge = self.overlay.get("graphic_eq", {}) or {}
        return float(ge.get("default_q", 4.318))

    @property
    def virtual_roles(self) -> List[str]:
        vc = self.overlay.get("virtual_channels", {}) or {}
        roles = vc.get("roles")
        if roles:
            return list(roles)
        # generic fallback: the discovered GLOBAL_EQ zones
        return list(self.topology.get("roles", {}).get("global_eq_zones", []))

    def inputs(self) -> List[InputChannel]:
        return [self.input(letter) for letter in self.input_letters]

    def outputs(self) -> List[OutputChannel]:
        return [self.output(letter) for letter in self.output_letters]

    def virtuals(self) -> List[VirtualChannel]:
        # Built from the overlay's explicit per-channel table (A..I incl. 'G', which is a
        # pass-through channel with no EQ). Models without that table have no virtual-EQ
        # channels here (graceful empty list).
        table = self._virtual_channel_table()
        return [self.virtual(key) for key in table] if table else []

    # -- channel factories ------------------------------------------------
    def input(self, letter: str) -> InputChannel:
        letter = letter.upper()
        if letter not in self.input_letters:
            raise ChannelModelError(f"no input '{letter}' (have {self.input_letters})")
        setup = (self.overlay.get("inputs", {}).get("labels", {}) or {}).get(letter)
        return InputChannel(self, letter, setup_label=setup)

    def output(self, letter: str) -> OutputChannel:
        letter = letter.upper()
        letters = self.output_letters
        if letter not in letters:
            raise ChannelModelError(f"no output '{letter}' (have {letters})")
        _out = self.overlay.get("outputs", {}) or {}
        # illustrative per-setup friendly names (Demo setup) — optional, never canonical.
        setup_labels = (_out.get("demo_setup_labels") or _out.get("default_labels")
                        or _out.get("provisional_static_labels") or {})
        return OutputChannel(self, letter, letters.index(letter), setup_label=setup_labels.get(letter))

    def virtual(self, selector: str) -> VirtualChannel:
        """Return a virtual (tuning) channel selected by EITHER its overlay letter key
        ('H') OR its label ('Subwoofer 1', case-insensitive). Driven by the overlay's
        ``virtual_channels.channels`` table (keys A..I; 'G' is a pass-through with no EQ)."""
        table = self._virtual_channel_table()
        if not table:
            # No per-channel table (other models): virtual-EQ channels are not defined here.
            raise ChannelModelError(
                f"model '{self.model_name}' has no virtual_channels.channels overlay table")
        keys = list(table.keys())
        sel = selector.strip()
        # 1) match by letter key (case-insensitive)
        for key in keys:
            if key.upper() == sel.upper():
                return self._make_virtual(key, table[key], keys.index(key))
        # 2) match by human label (case-insensitive)
        for key in keys:
            cfg = table[key] or {}
            if str(cfg.get("label", "")).strip().lower() == sel.lower():
                return self._make_virtual(key, cfg, keys.index(key))
        labels = [str((table[k] or {}).get("label", "")) for k in keys]
        raise ChannelModelError(
            f"no virtual channel '{selector}' (keys {keys}, labels {labels})")

    def _make_virtual(self, key: str, cfg: Optional[dict], index: int) -> VirtualChannel:
        cfg = cfg or {}
        return VirtualChannel(self, key,
                              list(cfg.get("eq_subblocks", []) or []), index,
                              pass_through=bool(cfg.get("pass_through", False)),
                              setup_label=cfg.get("label"))

    def _virtual_channel_table(self) -> Dict[str, dict]:
        vc = self.overlay.get("virtual_channels", {}) or {}
        return vc.get("channels", {}) or {}

    @property
    def routing(self) -> Routing:
        """Both routing matrices as a :class:`Routing` container (``input_to_virtual`` and
        ``virtual_to_output``), built from the overlay's ``routing.matrices`` block. A model
        overlay without that block degrades gracefully to an empty container."""
        if self._routing is None:
            self._routing = Routing(self, self._build_routing_matrices())
        return self._routing

    def _build_routing_matrices(self) -> Dict[str, RoutingMatrix]:
        routing_cfg = self.overlay.get("routing", {}) or {}
        matrices_cfg = routing_cfg.get("matrices", {}) or {}
        out: Dict[str, RoutingMatrix] = {}
        for name, cfg in matrices_cfg.items():
            cfg = cfg or {}
            template = cfg.get("template")
            dims = cfg.get("dims")
            if not template or not dims:
                continue
            out[name] = RoutingMatrix(
                model=self,
                name=name,
                template=template,
                rows_kind=str(cfg.get("rows", "")),
                cols_kind=str(cfg.get("cols", "")),
                dims=(int(dims[0]), int(dims[1])),
                confirmed=bool(cfg.get("confirmed", False)),
                sources=list(cfg.get("sources", []) or []) or None,
            )
        return out

    # -- index helpers ----------------------------------------------------
    def _output_index(self, letter: str) -> int:
        letters = self.output_letters
        letter = letter.upper()
        if letter not in letters:
            raise ChannelModelError(f"no output '{letter}'")
        return letters.index(letter)

    def _virtual_index(self, role: str) -> int:
        roles = self.virtual_roles
        for i, r in enumerate(roles):
            if r.lower() == role.lower():
                return i
        raise ChannelModelError(f"no virtual role '{role}' (have {roles})")

    def _virtual_axis_keys(self) -> List[str]:
        """Ordered virtual-axis keys (A..I incl. G) from the overlay channel table; falls
        back to the discovered GLOBAL_EQ zones / virtual roles when no table is present."""
        table = self._virtual_channel_table()
        if table:
            return list(table.keys())
        return list(self.virtual_roles)

    def _virtual_axis_index(self, selector) -> int:
        """Resolve a virtual selector (int, letter key 'A'..'I', or channel label) to its
        0-based position on the virtual routing axis (the overlay channel-table order)."""
        keys = self._virtual_axis_keys()
        if isinstance(selector, int):
            return selector
        sel = str(selector).strip()
        for i, k in enumerate(keys):
            if k.upper() == sel.upper():
                return i
        # try a human label via the virtual-channel table
        try:
            vc = self.virtual(sel)
        except ChannelModelError:
            vc = None
        if vc is not None and vc.key in keys:
            return keys.index(vc.key)
        raise ChannelModelError(f"no virtual axis selector '{selector}' (keys {keys})")

    def _input_source_index(self, selector, sources: Optional[List[str]]) -> int:
        """Resolve an input-source selector to its 0-based column. Accepts an int index, a
        source label from the overlay ``sources`` list (e.g. 'Input A', 'Digital In R',
        case-insensitive), or a bare input letter ('A' -> 'Input A')."""
        if isinstance(selector, int):
            return selector
        sel = str(selector).strip()
        if sources:
            for i, s in enumerate(sources):
                if s.strip().lower() == sel.lower():
                    return i
            if len(sel) == 1 and sel.isalpha():
                for i, s in enumerate(sources):
                    if s.strip().lower() == f"input {sel}".lower():
                        return i
        if len(sel) == 1 and sel.isalpha():
            return ord(sel.upper()) - ord("A")
        raise ChannelModelError(f"no input source '{selector}' (sources {sources})")

    # -- EQ block -> class mapping (DATA-DRIVEN from the overlay) ----------
    def _build_eq_class_map(self) -> Dict[str, List[tuple]]:
        """Map each channel class -> ordered [(block, band_count), ...] using the
        overlay's ``eq_blocks.<B>.assigned_to`` and discovered band counts. A class
        whose EQ spans two blocks (H-alt: output = EQ1+EQ2) falls out of this data
        automatically — no hypothesis is baked into code."""
        discovered = self.topology.get("eq_blocks", {})
        overlay_blocks = self.overlay.get("eq_blocks", {}) or {}
        out: Dict[str, List[tuple]] = {}
        for block, cfg in overlay_blocks.items():
            if block not in discovered:  # e.g. GLOBAL_EQ is a role, not a STAGE block
                continue
            cls = (cfg or {}).get("assigned_to")
            if cls is None:
                continue
            out.setdefault(cls, []).append((block, int(discovered[block]["bands"])))
        return out

    def _class_band_count(self, cls: str) -> int:
        return sum(bands for _, bands in self._eq_class_map.get(cls, []))

    def _class_eq_provisional(self, cls: str) -> bool:
        overlay_blocks = self.overlay.get("eq_blocks", {}) or {}
        blocks = [b for b, _ in self._eq_class_map.get(cls, [])]
        return any(bool((overlay_blocks.get(b) or {}).get("provisional", True)) for b in blocks) or not blocks

    def _band_to_block_stage(self, cls: str, i: int) -> tuple:
        # Two-block ordering (e.g. output = EQ1[15] + EQ2[16]) is ORDER-INDEPENDENT BY
        # CONSTRUCTION, not a fragile assumption: the bands are parametric peaking biquads
        # (f/Q/gain live in each stage's coeffs — no slot "owns" a frequency), and the blocks are
        # a plain cascade (PC-Tool "Outputs>Equalization", confirmed). A cascade of biquads
        # commutes, so which sub-block a band sits in has zero effect on the summed response. The
        # only requirements — all 31 stages addressable, no two bands colliding on one stage — hold
        # (Direction B 2026-08-29 round-tripped a band in EACH block: slots 2/10 in EQ1, 20 in EQ2;
        # oracle .pct6 round-tripped 40/250/2.5k). So the EQ1↔EQ2 order needs no further proof; it
        # would only matter if a block were special (pre/post another module, different format, or
        # independently bypassed) — nothing indicates that. See [[bench-session-2026-08-29]].
        mapping = self._eq_class_map.get(cls, [])
        if not mapping:
            raise ChannelModelError(
                f"no EQ block assigned to class '{cls}' in the overlay "
                f"(e.g. virtual-channel EQ is unconfirmed pending the PC-Tool 'Virtual' tab; "
                f"set eq_blocks.<BLOCK>.assigned_to: {cls} once known)")
        offset = 0
        for block, bands in mapping:
            if i < offset + bands:
                return block, i - offset
            offset += bands
        raise ChannelModelError(f"{cls} EQ band {i} out of range (0..{offset - 1})")

    # -- name resolution --------------------------------------------------
    def _require(self, name: str) -> int:
        addr = self.params.get(name)
        if addr is None:
            raise ChannelModelError(f"resolved param does not exist in the map: {name}")
        return addr

    def _eq_names(self, template: str, *, coeff_kwargs: dict) -> List[str]:
        return [template.format(coeff=coeff, **coeff_kwargs) for coeff in EQ_STORAGE_ORDER]

    def _resolve_input_eq(self, letter: str, i: int) -> EqBandRef:
        names = self._eq_names(self._templates["input_eq"], coeff_kwargs={"letter": letter, "band": i})
        base = names[0]
        addr = self._require(base)
        for n in names[1:]:
            self._require(n)
        return EqBandRef(names=names, base=base, base_addr=addr, block="INPUTEQ", stage=i, provisional=False)

    def _resolve_class_eq(self, cls: str, instance: int, i: int) -> EqBandRef:
        block, stage = self._band_to_block_stage(cls, i)
        template = self._templates[block.lower()]
        names = self._eq_names(template, coeff_kwargs={"inst": _inst(instance), "band": stage})
        base = names[0]
        addr = self._require(base)
        for n in names[1:]:
            self._require(n)
        return EqBandRef(names=names, base=base, base_addr=addr, block=block, stage=stage,
                         provisional=self._class_eq_provisional(cls))

    def _resolve_output_gain(self, index: int) -> ParamRef:
        # Output gain/level = the OUTPUTMUTE gain/mute cell (index 0 = output A). Supports either
        # an {index} template (current) or a legacy {letter}/wildcard form.
        template = self._templates["output_gain"]
        if "{index}" in template:
            name = template.format(index=index)
        else:
            letter = self.output_letters[index]
            template = template.replace("{letter}", letter)
            if "*" in template:
                prefix, suffix = template.split("*", 1)
                matches = [n for n in self.params.names() if n.startswith(prefix) and n.endswith(suffix)]
                if not matches:
                    raise ChannelModelError(f"no output-gain param matching {prefix}*{suffix}")
                name = matches[0]
            else:
                name = template
        return ParamRef(name=name, addr=self._require(name), provisional=False)

    def _resolve_virtual_gain(self, index: int) -> ParamRef:
        """Virtual channel gain/mute cell (VCP), index 0 = Virtual A (hw-confirmed)."""
        name = self._templates["virtual_gain"].format(index=index)
        return ParamRef(name=name, addr=self._require(name), provisional=False)

    def _resolve_output_delay(self, letter: str) -> ParamRef:
        name = self._templates["output_delay"].format(letter=letter)
        return ParamRef(name=name, addr=self._require(name), provisional=False)

    def _resolve_output_polarity(self, letter: str) -> ParamRef:
        """Resolve the per-output polarity/phase-invert cell. The template is a PREFIX wildcard
        (``MOD_DELAY__PHASE_SWITCH_INV{letter}_*``) because the INVERT sub-index after the letter
        is not a clean function of the letter; we match it against the param map. Marked
        ``provisional=True`` — the cell is confirmed but its ENCODING is not."""
        template = self._templates.get("output_polarity", _DEFAULT_TEMPLATES["output_polarity"])
        letter = letter.upper()
        if "*" in template:
            prefix, suffix = template.split("*", 1)
            prefix = prefix.format(letter=letter)
            suffix = suffix.format(letter=letter)
            matches = [n for n in self.params.names()
                       if n.startswith(prefix) and n.endswith(suffix)]
            if not matches:
                raise ChannelModelError(
                    f"no output-polarity param matching {prefix}*{suffix} (output {letter})")
            name = matches[0]
        else:
            name = template.format(letter=letter)
        return ParamRef(name=name, addr=self._require(name), provisional=True)

    def _resolve_output_eq_bypass(self, letter: str) -> ParamRef:
        """Resolve the per-output whole-EQ bypass mono-mux (prefix-wildcard, the NS# suffix varies
        per output). Hardware-confirmed 2026-08-31 (int32 index, 1 = bypass / 0 = active)."""
        template = self._templates.get("output_eq_bypass", _DEFAULT_TEMPLATES["output_eq_bypass"])
        letter = letter.upper()
        prefix, suffix = template.split("*", 1)
        prefix, suffix = prefix.format(letter=letter), suffix.format(letter=letter)
        matches = [n for n in self.params.names() if n.startswith(prefix) and n.endswith(suffix)]
        if not matches:
            raise ChannelModelError(
                f"no output-EQ-bypass param matching {prefix}*{suffix} (output {letter})")
        return ParamRef(name=matches[0], addr=self._require(matches[0]), provisional=False)

    def _resolve_virtual_eq_bypass(self, key: str) -> ParamRef:
        """Resolve a virtual channel's whole-EQ (GLOBAL_EQ) bypass mono-mux — the bypass cell for
        the region+side that its EQ bands already resolve into. Raises for a pass-through virtual
        (no EQ). Same encoding as the output bypass (int32 1=bypass/0=active)."""
        prefix = _VIRTUAL_EQ_BYPASS_PREFIX.get(key.upper())
        if prefix is None:
            raise ChannelModelError(f"virtual {key} has no GLOBAL_EQ bypass (pass-through / no EQ)")
        matches = [n for n in self.params.names() if n.startswith(prefix) and n.endswith("INDEX")]
        if not matches:
            raise ChannelModelError(f"no GLOBAL_EQ bypass param matching {prefix}*INDEX (virtual {key})")
        return ParamRef(name=matches[0], addr=self._require(matches[0]), provisional=False)

    def _resolve_output_mute(self, index: int) -> ParamRef:
        name = self._templates["output_mute"].format(index=index)
        return ParamRef(name=name, addr=self._require(name), provisional=False)

    def _resolve_input_mute(self, letter: str) -> ParamRef:
        index = self.input_letters.index(letter.upper())
        name = self._templates["input_mute"].format(index=index)
        return ParamRef(name=name, addr=self._require(name), provisional=False)

    def _resolve_output_xover(self, letter: str, stage: int) -> EqBandRef:
        # Resolve one FILTERS biquad stage (hw-confirmed: HP stages [0,1,2], LP [3,4,5]).
        template = self._templates["output_xover"]
        names = self._eq_names(template, coeff_kwargs={"letter": letter, "stage": stage})
        base = names[0]
        addr = self._require(base)
        for n in names[1:]:
            self._require(n)
        return EqBandRef(names=names, base=base, base_addr=addr, block="FILTERS", stage=stage, provisional=False)

    def _global_eq_stage_count(self, subblock: str) -> int:
        """Count the physical STAGE stages in a GLOBAL_EQ sub-block by probing the map
        (contiguous STAGE0.. whose B2 coeff exists). Data-driven; no hardcoded 15."""
        template = self._templates["global_eq"]
        n = 0
        while template.format(subblock=subblock, band=n, coeff=EQ_STORAGE_ORDER[0]) in self.params:
            n += 1
        return n

    def _resolve_virtual_eq(self, vc: "VirtualChannel", i: int) -> EqBandRef:
        """Map virtual EQ band *i* -> (sub-block, stage) by walking the channel's ordered
        ``eq_subblocks`` (band 0..14 -> sub-block[0] stage i; 15..29 -> sub-block[1] stage
        i-15), then build the 5 coeff param names via the ``global_eq`` template.

        NOTE (still unconfirmed, see overlay): WHICH sub-block is the low-frequency half —
        i.e. the exact band<->stage ordering across the 30-band span — is not yet verified;
        we take the overlay's listed order. The addressing/module + channel identity are
        confirmed (screenshot), so the resolved reference is non-provisional."""
        if not vc.eq_subblocks:
            raise ChannelModelError(
                f"virtual '{vc.label}' ({vc.key}) has no eq_subblocks in the overlay")
        template = self._templates["global_eq"]
        offset = 0
        for subblock in vc.eq_subblocks:
            bands = self._global_eq_stage_count(subblock)
            if i < offset + bands:
                stage = i - offset
                names = self._eq_names(template, coeff_kwargs={"subblock": subblock, "band": stage})
                base = names[0]
                addr = self._require(base)
                for n in names[1:]:
                    self._require(n)
                return EqBandRef(names=names, base=base, base_addr=addr, block=subblock,
                                 stage=stage, provisional=False)
            offset += bands
        raise ChannelModelError(
            f"virtual '{vc.label}' ({vc.key}) EQ band {i} out of range (0..{offset - 1})")

    # NOTE: virtual gain/mute resolution is intentionally NOT implemented — the
    # virtual-channel GAIN/MUTE param family is still unconfirmed (see the overlay's
    # virtual_channels note). VirtualChannel.gain/.mute raise ChannelModelError rather
    # than guess a target and write silently.

    def __repr__(self) -> str:
        return (f"ChannelModel(model={self.model_name!r}, inputs={len(self.input_letters)}, "
                f"outputs={len(self.output_letters)}, eq_blocks={list(self.eq_blocks)})")
