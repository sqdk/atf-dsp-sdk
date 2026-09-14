"""analyzer.py — Input Signal Analyzer (ISA) reader.  ***HARDWARE-VALIDATED 2026-08-29 (M 5.4DSP).***

This implements the DSP PC-Tool's electrical, on-chip **Input Signal Analyzer** (the ``InputRTA``
class in the exe), reverse-engineered statically in ``docs/input-analyzer-re.md`` (RE-0). It is NOT
the microphone/acoustic RTA.

Mechanism (from the PC-Tool disassembly): the analyzer is a **host-driven, stepped single-bandpass
sweep**, not an on-chip FFT. For each frequency point the tool

  1. selects/sums the input tap(s) via the NXMINPUTRTA mixer cells,
  2. retunes ONE analysis bandpass biquad (INPUTRTAFILTER1 target coeffs) — a ``0x03`` SafeLoad,
  3. waits an adaptive settle time (>= ~2 ms, longer at low frequency), then
  4. reads ONE RMS readback cell (RBINPUTRTA1) via ``0x02`` and decodes it as 8.24 fixed-point.

The frequency axis is therefore chosen HERE (host-side), exactly like the PC-Tool's "Frequency
Range" / RTA-points config; the DSP only offers a single tunable probe.

CONFIRMED ON HARDWARE (2026-08-29, M 5.4DSP bench — see [[bench-session-2026-08-29]]): the read
path (RBINPUTRTA1 responds, sane 8.24 values), signal detection (level rose −108→−58 dBFS with an
injected tone), the swept single-bandpass **mechanism**, and the **frequency map / calibration** —
a 1 kHz tone peaked at commanded 1000 Hz, a 3150 Hz tone at commanded 3200 Hz (~1.6 %), and the peak
tracked the tone across 1 k→4 k→3.15 k. ``select_input`` demonstrably switches taps (0/1 = the front
pair). This implicitly confirms the TARGB storage order and analysis-filter tuning (a wrong order
would not center correctly).

STILL APPROXIMATE (not blocking — the analyzer is used for input-EQ *shape* flattening): the exact
NXMINPUTRTA tap→input-LETTER mapping (only front/rear grouping is confirmed), and absolute dB
calibration (we report relative dBFS; a fixed offset to a reference suffices for flatten — no
absolute cal needed, per the bench decision). The write paths (channel select, filter retune, and
hence ``sweep``) are **un-gated** as of 2026-08-29 — they mutate only the analyzer's own probe
cells (not the audio path), and the mechanism is hardware-confirmed. The read-only level probe
(:meth:`InputAnalyzer.read_level` / :meth:`read_input_peak`) is side-effect-free.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import List, Optional, Sequence

from atf_dsp import encoding
from atf_dsp.device import Device
from atf_dsp.encoding import from_fixed, to_fixed

# ------------------------------------------------------------------ param names
# M 5.4DSP `.at01` names (resolved to addresses by the Device's ParamMap). See the findings doc.
RBINPUTRTA1 = "MOD_INPUTRTA_RBINPUTRTA1_READBACKALGNEWSIGMA3004VALUE"          # 4957 (RMS readback)
FILTER1_TARGB2_BASE = "MOD_INPUTRTA_INPUTRTAFILTER1_ALG0_EQS300MULTIDPSWSLEWALG1TARGB210"  # 39450..39454
NXMINPUTRTA_VOL = [
    f"MOD_INPUTRTA_NXMINPUTRTA_ALG0_NXNMIXS3004P6ALG1VOL000{i}" for i in range(4)  # 4940..4943
]
# Separate input LEVEL meter (broadband peak bar) — distinct feature, single channel:
INPUT_LEVEL_PEAK = "MOD_INPUT_LEVEL_PEAKREADBACK_ALG0_READBACKALGNEWSIGMA3008VALUE"  # 4944
INPUT_LEVEL_SELECT = "MOD_INPUT_LEVEL_CHANNELSELECT_ALG0_MONOMUXSIGMA300NS24INDEX"   # 8541
INPUT_LEVEL_RESET = "MOD_INPUT_LEVEL_RESETHOLD_ALG0_DCINPALG145X21VALUE"             # 8509

DBFS_FLOOR = -144.0
DEFAULT_FS = encoding.DEFAULT_FS  # 48000

# The exe uses a host QVector of points; we default to a log sweep. NOT the tool's exact list.
DEFAULT_F_LOW = 20.0
DEFAULT_F_HIGH = 20000.0
DEFAULT_POINTS = 61          # ~1/6-octave 20..20k
DEFAULT_Q = 4.318            # ~1/6-octave bandpass Q (Q = fc/BW; BW≈1/6 oct)


# ------------------------------------------------------------------ data types
@dataclass
class Spectrum:
    """A swept-analyzer result: parallel arrays of centre frequency (Hz) and level (dBFS)."""

    freqs: List[float]
    levels_dbfs: List[float]
    input_channel: Optional[int] = None
    raw_linear: List[float] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.freqs)

    def peak(self) -> tuple:
        """Return (freq_hz, level_dbfs) of the loudest bin (or (nan, floor) if empty)."""
        if not self.levels_dbfs:
            return (float("nan"), DBFS_FLOOR)
        i = max(range(len(self.levels_dbfs)), key=lambda k: self.levels_dbfs[k])
        return (self.freqs[i], self.levels_dbfs[i])


# ------------------------------------------------------------------ pure helpers
def linear_to_dbfs(value: float, floor: float = DBFS_FLOOR) -> float:
    """Linear magnitude (1.0 == full scale) → dBFS, floored for silence."""
    mag = abs(value)
    if mag <= 0:
        return floor
    return max(floor, 20.0 * math.log10(mag))


def log_sweep(f_low: float = DEFAULT_F_LOW, f_high: float = DEFAULT_F_HIGH,
              points: int = DEFAULT_POINTS) -> List[float]:
    """Log-spaced centre frequencies (inclusive of both ends)."""
    if points < 2 or f_low <= 0 or f_high <= f_low:
        raise ValueError("need points>=2 and 0 < f_low < f_high")
    r = math.log(f_high / f_low)
    return [f_low * math.exp(r * i / (points - 1)) for i in range(points)]


def bandpass_coeffs(f0: float, q: float = DEFAULT_Q, fs: int = DEFAULT_FS) -> List[int]:
    """RBJ constant-0dB-peak bandpass biquad → 5 stored 8.24 words in [B2,B1,B0,A2,A1] order.

    Uses the project-confirmed SigmaStudio storage convention (normalise by a0; store
    -a1/a0, -a2/a0). Hardware-validated 2026-08-29: sweeping this filter centres the passband on
    the commanded frequency (1 kHz→1000, 3150→3200), which confirms the TARGB storage order and
    the kind/Q are correct in practice; only the exact Q value is not independently pinned.
    """
    if q <= 0:
        raise ValueError("Q must be > 0")
    if not 0 < f0 < fs / 2:
        raise ValueError(f"f0 must be in (0, fs/2); got {f0} at fs={fs}")
    w0 = 2 * math.pi * f0 / fs
    cos_w0 = math.cos(w0)
    alpha = math.sin(w0) / (2 * q)
    # RBJ BPF (0 dB peak gain):
    b0, b1, b2 = alpha, 0.0, -alpha
    a0, a1, a2 = 1 + alpha, -2 * cos_w0, 1 - alpha
    stored = [b2 / a0, b1 / a0, b0 / a0, -a2 / a0, -a1 / a0]  # [B2,B1,B0,A2,A1]
    return [to_fixed(c) for c in stored]


def settle_ms(f0: float, min_ms: float = 2.0, cycles: float = 6.0) -> float:
    """Adaptive per-point settle delay (ms), matching the exe's shape: a floor (~2 ms) that
    grows toward low frequency. The exact firmware formula isn't reconstructed, but the default
    (max(min_ms, cycles periods of f0)) gave clean, stable sweeps on hardware (2026-08-29);
    default = max(min_ms, cycles periods of f0)."""
    return max(min_ms, cycles * 1000.0 / f0)


# ------------------------------------------------------------------ the reader
class InputAnalyzer:
    """Read the DSP's Input Signal Analyzer from a connected (or dry-run) :class:`Device`.

    Read-only probes are safe. The sweep and channel-select WRITE only to the analyzer's own probe
    cells (not the audio path); the mechanism is hardware-validated (2026-08-29) and un-gated.
    """

    def __init__(self, device: Device) -> None:
        self.device = device

    # -- read-only probes (side-effect free) ------------------------------
    def read_level(self, floor: float = DBFS_FLOOR) -> Optional[float]:
        """Read RBINPUTRTA1 once → dBFS (the RMS out of the analysis chain, at the filter's
        current tuning). Side-effect-free (``0x02`` read)."""
        return self._read_dbfs(RBINPUTRTA1, floor)

    def read_level_linear(self) -> Optional[float]:
        """Read RBINPUTRTA1 once → linear 8.24 magnitude (side-effect-free)."""
        return self._read_linear(RBINPUTRTA1)

    def read_input_peak(self, floor: float = DBFS_FLOOR) -> Optional[float]:
        """Read the *separate* broadband input LEVEL meter (INPUT_LEVEL PEAKREADBACK) → dBFS.
        Side-effect-free; this is the input level bar, not the analyzer spectrum."""
        return self._read_dbfs(INPUT_LEVEL_PEAK, floor)

    # -- writes (un-gated; analyzer probe cells, hw-validated mechanism) ------
    def select_input(self, taps: Sequence[int]) -> None:
        """Select which NXMINPUTRTA tap(s) feed the analyzer (unity = include, 0 = exclude).

        Un-gated (hw-validated 2026-08-29; writes only the analyzer's probe cells). Tap switching is
        hardware-confirmed; the exact tap→input-LETTER mapping is only grouped (0/1 = front pair) —
        not pinned per letter. ``taps`` are mixer cell indices 0..3 (VOL0000..0003)."""
        for i, name in enumerate(NXMINPUTRTA_VOL):
            raw = encoding.UNITY if i in taps else 0
            self.device.write_param(name, encoding.to_bytes_be(raw), safeload=True)

    def tune(self, f0: float, q: float = DEFAULT_Q, fs: Optional[int] = None) -> None:
        """Retune the analysis bandpass to *f0* (writes the 5 INPUTRTAFILTER1 target coeffs in
        one SafeLoad burst). Un-gated; centring hardware-confirmed 2026-08-29. *fs* defaults to
        the device's processing rate so the bandpass centres correctly on 96/192 kHz firmware."""
        coeffs = bandpass_coeffs(f0, q=q, fs=self.device.fs if fs is None else fs)
        data = b"".join(encoding.to_bytes_be(c) for c in coeffs)
        self.device.write_param(FILTER1_TARGB2_BASE, data, safeload=True)

    def sweep(self, freqs: Optional[Sequence[float]] = None, *, q: float = DEFAULT_Q,
              fs: Optional[int] = None, settle: Optional[float] = None, floor: float = DBFS_FLOOR,
              input_channel: Optional[int] = None,
              sleep=time.sleep) -> Spectrum:
        """Full swept spectrum: for each frequency, retune the bandpass, wait, read RBINPUTRTA1.

        Un-gated (writes the analyzer probe filter). Hardware-validated 2026-08-29 — the peak
        tracks the injected tone. ``settle`` overrides the adaptive per-point delay (ms); ``sleep``
        is injectable for tests. Returns a :class:`Spectrum`.
        """
        pts = list(freqs) if freqs is not None else log_sweep()
        out_lin: List[float] = []
        out_db: List[float] = []
        for f0 in pts:
            self.tune(f0, q=q, fs=fs)
            sleep((settle if settle is not None else settle_ms(f0)) / 1000.0)
            lin = self._read_linear(RBINPUTRTA1)
            lin = 0.0 if lin is None else lin
            out_lin.append(lin)
            out_db.append(linear_to_dbfs(lin, floor))
        return Spectrum(freqs=pts, levels_dbfs=out_db, input_channel=input_channel, raw_linear=out_lin)

    # -- internals --------------------------------------------------------
    def _read_linear(self, name: str) -> Optional[float]:
        data = self.device.read_param(name, nbytes=4)
        if data is None or len(data) < 4:
            return None
        return from_fixed(int.from_bytes(data[:4], "big"))

    def _read_dbfs(self, name: str, floor: float) -> Optional[float]:
        lin = self._read_linear(name)
        return None if lin is None else linear_to_dbfs(lin, floor)


# ------------------------------------------------------------------ convenience
def read_input(device: Device, input_channel: Optional[int] = None, *, sweep: bool = False,
               freqs: Optional[Sequence[float]] = None, **kw):
    """Read the Input Signal Analyzer for *input_channel*.

    - Default (safe, read-only): returns a ``dict`` of the current levels
      ``{"analyzer_rms_dbfs": .., "input_peak_dbfs": ..}`` — a single-point probe, no writes.
    - ``sweep=True``: selects the input tap, runs the swept bandpass, and returns a
      :class:`Spectrum`.

    Hardware-validated 2026-08-29 (mechanism + frequency map); see ``docs/input-analyzer-re.md``.
    """
    an = InputAnalyzer(device)
    if sweep:
        if input_channel is not None:
            an.select_input([input_channel])
        return an.sweep(freqs=freqs, input_channel=input_channel, **kw)
    return {
        "analyzer_rms_dbfs": an.read_level(),
        "input_peak_dbfs": an.read_input_peak(),
    }
