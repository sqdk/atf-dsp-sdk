"""validate.py — an end-to-end validation harness for the acodsp control surface.

The goal is to PROVE, against real hardware with the DSP PC-Tool as the independent oracle,
that we SET and READ every control correctly. Two directions, deliberately separated because
they close different gaps:

* **Direction B — SDK writes -> SDK reads (:func:`exercise_all`).** Apply a comprehensive,
  deterministic :class:`TestVector` through the acodsp control setters, then read every cell
  back off the DSP, decode it, and assert it matches within tolerance. This proves *writes
  persist*, *encode/decode are inverse*, and *SafeLoad works* — but it is PARTIALLY CIRCULAR:
  it uses our own encoders on both ends, so it does NOT prove a cell is the semantically-correct
  one, nor that the PC-Tool agrees. It runs GREEN offline against the memory-backed dry-run
  ``Link(mem=…)`` (writes round-trip) and unchanged on real hardware (wrapped in snapshot/restore).

* **Direction A — PC-Tool writes -> SDK reads (:func:`assert_matches_pct6`).** Compare a
  PC-Tool-saved ``.pct6`` (the independent oracle: the PC-Tool ENCODED it) against the same
  known :class:`TestVector`, tolerance-aware. This validates our ``.pct6`` / ``from_device``
  DECODE against the PC-Tool's ENCODE — the leg :func:`exercise_all` structurally cannot cover.

All comparisons are TOLERANCE-AWARE, never exact XML/text: an EQ band is stored as biquad
coefficients and f/Q/gain is RECOVERED from them (rounding); frequencies/gains quantize (8.24
fixed point), delays are integer samples. Only DSP-BACKED fields are compared — the lossy
``.pct6`` fields (CN role codes, channel names, UI flags) are skipped/annotated, never asserted.

Importable with NO hardware.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from acodsp import encoding

# ---------------------------------------------------------------------------
# tolerances
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Tolerance:
    """Per-unit comparison tolerances. Frequencies allow the LARGER of an absolute Hz band
    or a relative percentage (low corners want Hz, high corners want %); everything else is
    an absolute band. Chosen to swallow 8.24 rounding + biquad-inverse recovery error, not to
    paper over real mismatches."""

    freq_hz: float = 0.5            # ±0.5 Hz absolute
    freq_pct: float = 0.01         # or ±1% relative (whichever is larger)
    q: float = 0.02               # ±0.02 Q
    gain_db: float = 0.1          # ±0.1 dB
    delay_samples: int = 1        # ±1 sample
    route_db: float = 0.1         # ±0.1 dB on a routing mix cell


DEFAULT_TOL = Tolerance()


def approx_hz(actual: float, expected: float, tol: Tolerance = DEFAULT_TOL) -> bool:
    """True when a frequency is within max(±freq_hz, ±freq_pct·|expected|)."""
    return abs(actual - expected) <= max(tol.freq_hz, tol.freq_pct * abs(expected))


def approx(actual: float, expected: float, atol: float) -> bool:
    """True when ``|actual-expected| <= atol`` (both finite)."""
    return abs(actual - expected) <= atol


# ---------------------------------------------------------------------------
# the test vector
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class EqSpec:
    """One EQ band to set + verify (peaking — the invertible/recoverable kind)."""

    band: int
    f: float
    q: float
    gain_db: float
    kind: str = "peaking"


@dataclass(frozen=True)
class CrossoverSpec:
    """An output crossover: HP and/or LP corner (Hz, None to skip), characteristic + slope."""

    hp: Optional[float]
    lp: Optional[float]
    characteristic: str            # 'butterworth' | 'linkwitz_riley'
    slope: int                     # 12 | 24


@dataclass(frozen=True)
class OutputVec:
    letter: str
    gain_db: float
    mute: bool
    delay_ms: float
    eq: List[EqSpec]
    crossover: Optional[CrossoverSpec]
    polarity: bool = False         # per-output phase-invert (PROVISIONAL ±1.0 encoding -> .pct6 CINV)


@dataclass(frozen=True)
class VirtualVec:
    index: int                     # position in model.virtuals()
    key: str
    gain_db: float
    mute: bool
    eq: List[EqSpec]


@dataclass(frozen=True)
class InputVec:
    letter: str
    eq: List[EqSpec]


@dataclass(frozen=True)
class RouteCell:
    matrix: str                    # 'input_to_virtual' | 'virtual_to_output'
    row: int
    col: int
    gain_db: float


@dataclass(frozen=True)
class TestVector:
    """A comprehensive, deterministic tuning state: a DISTINCT value per channel per control,
    so a mis-mapped cell (wrong channel / wrong band) is caught by a value mismatch."""

    outputs: List[OutputVec]
    virtuals: List[VirtualVec]
    inputs: List[InputVec]
    routes: List[RouteCell]
    fs: int = encoding.DEFAULT_FS


# Deterministic per-channel EQ band positions + values. Distinct across channels AND bands.
_EQ_BAND_SLOTS = (2, 10, 20)          # low / mid / high graphic slots
_EQ_Q_BASE = (0.7, 1.5, 3.0)
_EQ_GAIN_BASE = (-4.0, 3.0, -2.0)     # all clearly non-zero (0 dB peaking stores as bypass)


def _eq_specs_for(channel_ordinal: int, band_frequency, band_count: int) -> List[EqSpec]:
    """Build the distinct EQ bands for one channel: three peaking bands whose f/Q/gain all
    vary with the channel ordinal so no two channels/bands share a value."""
    specs: List[EqSpec] = []
    for j, slot in enumerate(_EQ_BAND_SLOTS):
        if slot >= band_count:
            continue
        f = float(band_frequency(slot))
        q = round(_EQ_Q_BASE[j] + 0.10 * channel_ordinal, 4)
        gain = round(_EQ_GAIN_BASE[j] + 0.25 * channel_ordinal, 3)
        if abs(gain) < 0.25:           # never land on 0 dB (bypass image)
            gain += 0.5
        specs.append(EqSpec(band=slot, f=f, q=q, gain_db=gain))
    return specs


def build_test_vector(model) -> TestVector:
    """A comprehensive, DETERMINISTIC test vector for *model* — every channel gets a distinct
    value per control so a mis-mapped cell is caught.

    Coverage: per output — gain, mute (a spread muted), delay, three distinct EQ bands, and a
    crossover (HP-only / LP-only / both alternate so 'off' sections are exercised, with the
    characteristic + slope varied across the confirmed table); per virtual — gain, mute, three
    EQ bands (pass-through virtuals get gain/mute only); per input — EQ bands; and a spread of
    cells in BOTH routing matrices. Values are functions of the channel/band index, so the
    vector is stable run-to-run and independent of any hardware.
    """
    chars = ("butterworth", "linkwitz_riley")

    outputs: List[OutputVec] = []
    for oi, letter in enumerate(model.output_letters):
        oc = model.output(letter)
        muted = (oi % 4 == 3)                      # a spread of muted outputs (e.g. D, H)
        gain = round(-1.0 - 0.5 * oi, 3)           # distinct level per output
        delay = round(0.5 * (oi + 1), 3)           # distinct ms per output (-> distinct samples)
        eq = _eq_specs_for(oi, oc.band_frequency, oc.eq_band_count)
        # crossover: rotate HP-only / LP-only / both so off-section recovery is exercised.
        char = chars[oi % 2]
        slope = 12 if (oi % 3 == 1) else 24
        hp = round(60.0 + 10.0 * oi, 2)
        lp = round(2000.0 + 250.0 * oi, 2)
        mode = oi % 3
        xover = CrossoverSpec(hp=(hp if mode != 2 else None),
                              lp=(lp if mode != 1 else None),
                              characteristic=char, slope=slope)
        # inverted outputs (polarity = the gain-cell sign). NOT on muted outputs: mute zeroes the
        # shared cell, so polarity is unrecoverable there (would be a false mismatch).
        polarity = (oi % 3 == 0) and not muted
        outputs.append(OutputVec(letter=letter, gain_db=gain, mute=muted,
                                 delay_ms=delay, eq=eq, crossover=xover, polarity=polarity))

    virtuals: List[VirtualVec] = []
    for vi, vc in enumerate(model.virtuals()):
        muted = (vi % 5 == 4)
        gain = round(-2.0 - 0.5 * vi, 3)
        eq = ([] if vc.pass_through
              else _eq_specs_for(vi, vc.band_frequency, vc.eq_band_count))
        virtuals.append(VirtualVec(index=vi, key=vc.key, gain_db=gain, mute=muted, eq=eq))

    inputs: List[InputVec] = []
    for ii, letter in enumerate(model.input_letters):
        ic = model.input(letter)
        # inputs have no graphic-frequency table; use the shared graphic freqs as sensible f's.
        eq = _eq_specs_for(ii + 1, model.output("A").band_frequency, ic.eq_band_count)
        inputs.append(InputVec(letter=letter, eq=eq))

    routes: List[RouteCell] = []
    route_db_cycle = (0.0, -1.5, -3.0, -4.5, -6.0)

    # Main->Virtual (MAINMATRIX / input_to_virtual): route each NAMED Main input A..D to a
    # distinct virtual row so each lands in its REAL DSP column. Decoded vs the PC-Tool oracle:
    # Main A/B/C/D = cols 2/3/4/5 (cols 0/1 are the digital pair). The column is resolved through
    # the matrix's `sources` column map (position == DSP column) — never a raw +2 offset — so a
    # RouteCell.col is the actual DSP column and the .pct6 G-index (row*INS + col) stays correct.
    m2v = getattr(model.routing, "input_to_virtual", None)
    if m2v is not None:
        for li, letter in enumerate(model.input_letters):        # A..D -> virtual rows 0..3
            src = f"Input {letter}"
            if not m2v.sources or src not in m2v.sources:
                continue
            col = m2v.sources.index(src)                          # DSP column (A->2, B->3, ...)
            row = li % m2v.dims[0]
            if m2v.cell_param(row, col) not in model.params:
                continue
            routes.append(RouteCell(matrix="input_to_virtual", row=row, col=col,
                                    gain_db=route_db_cycle[li % len(route_db_cycle)]))

    # Digital->Virtual (OPTOMIXER / digital_to_virtual): exercise the two digital sources (cols
    # 0/1) on two virtual rows. Digital routing belongs in THIS block, not the Main matrix.
    d2v = getattr(model.routing, "digital_to_virtual", None)
    if d2v is not None:
        for ci in range(d2v.dims[1]):                            # digital L/R
            if d2v.cell_param(ci, ci) not in model.params:
                continue
            routes.append(RouteCell(matrix="digital_to_virtual", row=ci, col=ci,
                                    gain_db=route_db_cycle[(ci + 1) % len(route_db_cycle)]))

    # Virtual->Output: a diagonal + a couple off-diagonal cells (generic positional spread).
    v2o = getattr(model.routing, "virtual_to_output", None)
    if v2o is not None:
        rows, cols = v2o.dims
        k = 0
        for r in range(min(rows, cols)):
            for (rr, cc) in ((r, r), (r, (r + 1) % cols)):
                pname = v2o.template.format(row=f"{rr:02d}", col=f"{cc:02d}")
                if pname not in model.params:
                    continue
                routes.append(RouteCell(matrix="virtual_to_output", row=rr, col=cc,
                                        gain_db=route_db_cycle[k % len(route_db_cycle)]))
                k += 1
                if k >= 5:
                    break
            if k >= 5:
                break

    return TestVector(outputs=outputs, virtuals=virtuals, inputs=inputs, routes=routes,
                      fs=encoding.DEFAULT_FS)


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------
@dataclass
class Check:
    """One control's pass/fail with actual vs expected (and an optional note)."""

    control: str
    ok: bool
    expected: Any
    actual: Any
    detail: str = ""

    def __str__(self) -> str:
        flag = "PASS" if self.ok else "FAIL"
        base = f"[{flag}] {self.control}: expected={self.expected!r} actual={self.actual!r}"
        return base + (f" ({self.detail})" if self.detail else "")


@dataclass
class Report:
    """Structured result of a validation pass: a list of per-control :class:`Check`s plus a
    list of skipped (lossy / not-applicable) fields with reasons."""

    direction: str
    checks: List[Check] = field(default_factory=list)
    skipped: List[str] = field(default_factory=list)

    def add(self, control: str, ok: bool, expected: Any, actual: Any, detail: str = "") -> None:
        self.checks.append(Check(control, ok, expected, actual, detail))

    def skip(self, what: str) -> None:
        self.skipped.append(what)

    @property
    def ok(self) -> bool:
        return all(c.ok for c in self.checks)

    @property
    def passed(self) -> int:
        return sum(1 for c in self.checks if c.ok)

    @property
    def failed(self) -> int:
        return sum(1 for c in self.checks if not c.ok)

    def failures(self) -> List[Check]:
        return [c for c in self.checks if not c.ok]

    def __bool__(self) -> bool:
        return self.ok

    def summary(self) -> str:
        head = (f"{self.direction}: {self.passed}/{len(self.checks)} checks passed"
                f"{' — ALL OK' if self.ok else f' — {self.failed} FAILED'}")
        lines = [head]
        for c in self.failures():
            lines.append("  " + str(c))
        if self.skipped:
            lines.append(f"  (skipped {len(self.skipped)} lossy/N-A field(s): "
                         f"{', '.join(self.skipped[:6])}{' …' if len(self.skipped) > 6 else ''})")
        return "\n".join(lines)

    def raise_for_status(self) -> "Report":
        if not self.ok:
            raise AssertionError(self.summary())
        return self


# ---------------------------------------------------------------------------
# small readback decoders
# ---------------------------------------------------------------------------
def _read_word(device, name: str) -> Optional[int]:
    data = device.read_param(name, nbytes=4)
    if not data or len(data) < 4:
        return None
    return int.from_bytes(data[:4], "big")


def _read_gain_db(device, name: str) -> Optional[float]:
    raw = _read_word(device, name)
    if raw is None:
        return None
    lin = encoding.from_fixed(raw)
    if lin == 0:
        return None                 # muted: no dB value
    return 20.0 * math.log10(abs(lin))   # MAGNITUDE (polarity is the sign, read separately)


def _read_eq_recovered(device, base: str):
    data = device.read_param(base, nbytes=20)
    words = [int.from_bytes(data[j:j + 4], "big") for j in range(0, min(len(data), 20), 4)]
    if len(words) != 5:
        return None
    return encoding.peaking_from_coeffs(words)


# ---------------------------------------------------------------------------
# Direction B — exercise_all (SDK writes -> SDK reads)
# ---------------------------------------------------------------------------
def _all_targets(device, vector: TestVector) -> List[Tuple[str, int]]:
    """Every (param_name, byte_width) the vector will write — for snapshot/restore."""
    model = device.model
    targets: List[Tuple[str, int]] = []
    for ov in vector.outputs:
        oc = model.output(ov.letter)
        targets.append((oc.resolve_gain().name, 4))
        targets.append((oc.resolve_delay().name, 4))
        try:
            targets.append((oc.resolve_polarity().name, 4))
        except Exception:
            pass
        for spec in ov.eq:
            targets.append((oc.resolve_eq_band(spec.band).base, 20))
        if ov.crossover is not None:
            from acodsp.channels import _XOVER_STAGES
            for kind in ("highpass", "lowpass"):
                # snapshot EVERY stage the write touches (active + the bypassed rest), so
                # snapshot/restore fully covers a section (up to 3 stages for 36 dB).
                for pos in range(len(_XOVER_STAGES[kind])):
                    try:
                        targets.append((oc.resolve_crossover(kind, pos).base, 20))
                    except Exception:
                        pass
    for vv in vector.virtuals:
        vc = model.virtuals()[vv.index]
        targets.append((vc.resolve_gain().name, 4))
        for spec in vv.eq:
            targets.append((vc.resolve_eq_band(spec.band).base, 20))
    for iv in vector.inputs:
        ic = model.input(iv.letter)
        for spec in iv.eq:
            targets.append((ic.resolve_eq_band(spec.band).base, 20))
    for rc in vector.routes:
        mtx = getattr(model.routing, rc.matrix)
        targets.append((mtx.cell_param(rc.row, rc.col), 4))
    # de-dup preserving first width
    seen: Dict[str, int] = {}
    for name, width in targets:
        seen.setdefault(name, width)
    return list(seen.items())


def _snapshot(device, targets: List[Tuple[str, int]]) -> Dict[str, bytes]:
    return {name: device.read_param(name, nbytes=width) for name, width in targets}


def _restore(device, snap: Dict[str, bytes]) -> None:
    for name, data in snap.items():
        try:
            device.write_param(name, data, safeload=True)
        except Exception:
            pass


def exercise_all(device, vector: Optional[TestVector] = None, *, allow_unsafe: bool = True,
                 snapshot: bool = True, tol: Tolerance = DEFAULT_TOL) -> Report:
    """DIRECTION B — apply the whole *vector* via the acodsp control setters, then read every
    cell back, decode it, and assert it is within *tolerance*.

    Proves (on the memory-backed dry-run device AND on real hardware): writes PERSIST,
    encode/decode are INVERSE, and the firmware SafeLoad path works. Circularity boundary: both
    the write and the read use OUR encoders, so this does NOT prove a cell is the
    semantically-correct one or that the PC-Tool agrees — that is Direction A
    (:func:`assert_matches_pct6`).

    ``allow_unsafe`` passes ``unsafe=True`` to the gated encoders (delay/crossover/routing).
    ``snapshot`` snapshots every touched cell FIRST and restores it after readback (so a hardware
    run leaves the amp exactly as found); set it False to let the writes PERSIST (used for the
    ``from_device`` round-trip and to stage a real test setup). Returns a structured
    :class:`Report`.
    """
    model = device.model
    if vector is None:
        vector = build_test_vector(model)

    snap: Dict[str, bytes] = {}
    if snapshot:
        snap = _snapshot(device, _all_targets(device, vector))

    rep = Report(direction="Direction B (SDK write -> SDK read)")
    try:
        _apply_vector(model, vector, allow_unsafe=allow_unsafe)
        _readback_and_check(device, vector, rep, tol)
    finally:
        if snapshot:
            _restore(device, snap)
    return rep


def _apply_vector(model, vector: TestVector, *, allow_unsafe: bool) -> None:
    for ov in vector.outputs:
        oc = model.output(ov.letter)
        # gain / polarity / mute are ONE cell (value = sign × magnitude, 0 = mute) — write once.
        if ov.mute:
            oc.mute(True)
        else:
            oc.gain(ov.gain_db, inverted=ov.polarity)   # sign = polarity, magnitude = gain
        oc.delay_ms(ov.delay_ms, unsafe=allow_unsafe)
        for spec in ov.eq:
            oc.eq_band(spec.band, spec.f, spec.q, spec.gain_db, kind=spec.kind)
        if ov.crossover is not None:
            oc.crossover(hp=ov.crossover.hp, lp=ov.crossover.lp,
                         characteristic=ov.crossover.characteristic,
                         slope=ov.crossover.slope, unsafe=allow_unsafe)
    for vv in vector.virtuals:
        vc = model.virtuals()[vv.index]
        if vv.mute:
            vc.mute(True)
        else:
            vc.gain(vv.gain_db)
        for spec in vv.eq:
            vc.eq_band(spec.band, spec.f, spec.q, spec.gain_db, kind=spec.kind)
    for iv in vector.inputs:
        ic = model.input(iv.letter)
        for spec in iv.eq:
            ic.eq_band(spec.band, spec.f, spec.q, spec.gain_db, kind=spec.kind)
    for rc in vector.routes:
        getattr(model.routing, rc.matrix).set(rc.row, rc.col, rc.gain_db, unsafe=allow_unsafe)


def _check_eq_readback(device, resolve, specs, label: str, rep: Report, tol: Tolerance) -> None:
    for spec in specs:
        base = resolve(spec.band).base
        rec = _read_eq_recovered(device, base)
        ctrl = f"{label} EQ band {spec.band}"
        if rec is None:
            rep.add(ctrl, False, (spec.f, spec.q, spec.gain_db), None, "not recovered (bypass?)")
            continue
        f, q, g = rec
        ok = approx_hz(f, spec.f, tol) and approx(q, spec.q, tol.q) and approx(g, spec.gain_db, tol.gain_db)
        rep.add(ctrl, ok, (round(spec.f, 2), spec.q, spec.gain_db),
                (round(f, 2), round(q, 4), round(g, 3)))


def _readback_and_check(device, vector: TestVector, rep: Report, tol: Tolerance) -> None:
    model = device.model
    for ov in vector.outputs:
        oc = model.output(ov.letter)
        # gain / mute (shared cell)
        raw = _read_word(device, oc.resolve_gain().name)
        if ov.mute:
            lin = encoding.from_fixed(raw) if raw is not None else None
            rep.add(f"Output {ov.letter} mute", lin is not None and abs(lin) < 1e-9, True,
                    None if raw is None else round(encoding.from_fixed(raw), 6))
        else:
            g = _read_gain_db(device, oc.resolve_gain().name)
            rep.add(f"Output {ov.letter} gain", g is not None and approx(g, ov.gain_db, tol.gain_db),
                    ov.gain_db, None if g is None else round(g, 3))
        # delay
        raw = _read_word(device, oc.resolve_delay().name)
        exp_samples = encoding.delay_ms_to_samples(ov.delay_ms, fs=vector.fs)
        rep.add(f"Output {ov.letter} delay", raw is not None and abs(raw - exp_samples) <= tol.delay_samples,
                exp_samples, raw, f"{ov.delay_ms} ms")
        # polarity = the SIGN of the gain cell (−ve == inverted). A muted cell (0) carries no
        # sign, so polarity is only checked on un-muted outputs.
        if not ov.mute:
            praw = _read_word(device, oc.resolve_gain().name)   # polarity = gain-cell sign
            got_inv = None if praw is None else (encoding.from_fixed(praw) < 0)
            rep.add(f"Output {ov.letter} polarity", got_inv is not None and got_inv == ov.polarity,
                ov.polarity, got_inv)
        # EQ
        _check_eq_readback(device, oc.resolve_eq_band, ov.eq, f"Output {ov.letter}", rep, tol)
        # crossover
        if ov.crossover is not None:
            _check_crossover(oc.recover_crossover(fs=vector.fs), ov.crossover,
                             f"Output {ov.letter}", rep, tol)

    for vv in vector.virtuals:
        vc = model.virtuals()[vv.index]
        raw = _read_word(device, vc.resolve_gain().name)
        if vv.mute:
            lin = encoding.from_fixed(raw) if raw is not None else None
            rep.add(f"Virtual {vv.key} mute", lin is not None and abs(lin) < 1e-9, True,
                    None if raw is None else round(encoding.from_fixed(raw), 6))
        else:
            g = _read_gain_db(device, vc.resolve_gain().name)
            rep.add(f"Virtual {vv.key} gain", g is not None and approx(g, vv.gain_db, tol.gain_db),
                    vv.gain_db, None if g is None else round(g, 3))
        _check_eq_readback(device, vc.resolve_eq_band, vv.eq, f"Virtual {vv.key}", rep, tol)

    for iv in vector.inputs:
        ic = model.input(iv.letter)
        _check_eq_readback(device, ic.resolve_eq_band, iv.eq, f"Input {iv.letter}", rep, tol)

    for rc in vector.routes:
        mtx = getattr(model.routing, rc.matrix)
        g = _read_gain_db(device, mtx.cell_param(rc.row, rc.col))
        ctrl = f"Route {rc.matrix}[{rc.row},{rc.col}]"
        rep.add(ctrl, g is not None and approx(g, rc.gain_db, tol.route_db),
                rc.gain_db, None if g is None else round(g, 3))


def _check_crossover(recovered: dict, spec: CrossoverSpec, label: str, rep: Report,
                     tol: Tolerance) -> None:
    from acodsp.channels import _infer_crossover_characteristic  # noqa: F401 (kept for parity)

    for kind, corner in (("highpass", spec.hp), ("lowpass", spec.lp)):
        rec = recovered.get(kind)
        ctrl = f"{label} xover {kind}"
        if corner is None:
            rep.add(ctrl, rec is not None and rec.off, "off",
                    None if rec is None else ("off" if rec.off else round(rec.corner_hz or 0, 2)))
            continue
        if rec is None or rec.off or rec.corner_hz is None:
            rep.add(ctrl, False, (corner, spec.characteristic, spec.slope), None)
            continue
        ok = (approx_hz(rec.corner_hz, corner, tol)
              and rec.slope_db == spec.slope
              and rec.characteristic == spec.characteristic)
        rep.add(ctrl, ok, (corner, spec.characteristic, spec.slope),
                (round(rec.corner_hz, 2), rec.characteristic, rec.slope_db))


# ---------------------------------------------------------------------------
# Direction A — assert_matches_pct6 (PC-Tool writes -> SDK reads)
# ---------------------------------------------------------------------------
_LOSSY_PCT6_FIELDS = (
    "channel role code CN", "channel display names", "EqBy/CE/UI flags",
    "routing AOM/OFFS metadata",
)

# from_device emits routing blocks in this order (Main, Digital, Virtual->Output).
_ROUTE_ORDER = ("input_to_virtual", "digital_to_virtual", "virtual_to_output")


def _pct6_channel_gain_db(ch) -> Optional[float]:
    return ch.gain_db


def assert_matches_pct6(setup_or_path, vector: TestVector, *,
                        tol: Tolerance = DEFAULT_TOL) -> Report:
    """DIRECTION A — compare a ``.pct6`` (the independent PC-Tool oracle) against the known
    *vector*, tolerance-aware, and report mismatches.

    ``setup_or_path`` is a :class:`acodsp.pct6.Setup` or a path to a ``.pct6``/``.afpx`` file.
    Validates our ``.pct6``/``from_device`` DECODE against the PC-Tool's ENCODE for the
    DSP-BACKED fields only: per-channel gain/mute, delay, peaking EQ (f/Q/gain recovered from the
    stored biquads), the recovered crossover corner+slope+characteristic, and both routing
    matrices. The known-LOSSY ``.pct6`` fields (CN role codes, channel names, EqBy/CE/UI flags,
    routing AOM/OFFS) are SKIPPED and annotated — never asserted (the PC-Tool owns those; they
    are not DSP RAM params). Returns a structured :class:`Report`.
    """
    from acodsp.pct6 import Setup

    setup = setup_or_path if isinstance(setup_or_path, Setup) else Setup.load(setup_or_path)
    rep = Report(direction="Direction A (PC-Tool .pct6 -> SDK read)")
    for f in _LOSSY_PCT6_FIELDS:
        rep.skip(f)

    # outputs -----------------------------------------------------------------
    for idx, ov in enumerate(vector.outputs):
        if idx >= len(setup.outputs):
            rep.add(f"Output {ov.letter}", False, "present", "missing")
            continue
        ch = setup.outputs[idx]
        _check_pct6_gain_mute(ch, ov.mute, ov.gain_db, f"Output {ov.letter}", rep, tol)
        _check_pct6_delay(ch, ov.delay_ms, vector.fs, f"Output {ov.letter}", rep, tol)
        _check_pct6_eq(ch, ov.eq, f"Output {ov.letter}", rep, tol)
        if ov.crossover is not None:
            _check_pct6_crossover(ch, ov.crossover, f"Output {ov.letter}", rep, tol)
        # polarity -> .pct6 CINV (0 normal / 1 inverted). The CINV field is validated here;
        # the underlying ±1.0 DSP encoding stays PROVISIONAL (unvalidated on hardware).
        try:
            cinv = int(ch.raw.get("CINV")) if ch.raw.get("CINV") is not None else None
        except (TypeError, ValueError):
            cinv = None
        rep.add(f"Output {ov.letter} polarity",
                cinv is not None and bool(cinv) == ov.polarity, ov.polarity,
                None if cinv is None else bool(cinv))

    # virtuals ---------------------------------------------------------------
    for vv in vector.virtuals:
        if vv.index < len(setup.virtuals):
            ch = setup.virtuals[vv.index]
        else:
            rep.add(f"Virtual {vv.key}", False, "present", "missing")
            continue
        _check_pct6_gain_mute(ch, vv.mute, vv.gain_db, f"Virtual {vv.key}", rep, tol)
        _check_pct6_eq(ch, vv.eq, f"Virtual {vv.key}", rep, tol)

    # inputs -----------------------------------------------------------------
    for ii, iv in enumerate(vector.inputs):
        if ii < len(setup.inputs):
            _check_pct6_eq(setup.inputs[ii], iv.eq, f"Input {iv.letter}", rep, tol)
        else:
            rep.add(f"Input {iv.letter}", False, "present", "missing")

    # routing ----------------------------------------------------------------
    _check_pct6_routing(setup, vector, rep, tol)
    return rep


def _check_pct6_gain_mute(ch, mute: bool, gain_db: float, label: str, rep: Report,
                          tol: Tolerance) -> None:
    if mute:
        # Mute + the pre-mute gain aren't recoverable from a muted DSP cell (gain/mute share
        # the OUTPUTMUTE cell — muted reads linear 0). from_device writes unity (0 dB) to avoid
        # a PC-Tool crash on Vol L=0, so neither mute nor gain survives the .pct6 round-trip.
        rep.skip(f"{label} gain/mute (muted: not .pct6-recoverable)")
    else:
        g = _pct6_channel_gain_db(ch)
        rep.add(f"{label} gain", g is not None and approx(g, gain_db, tol.gain_db),
                gain_db, None if g is None else round(g, 3))


def _check_pct6_delay(ch, delay_ms: float, fs: int, label: str, rep: Report, tol: Tolerance) -> None:
    exp = encoding.delay_ms_to_samples(delay_ms, fs=fs)
    raw = None
    # Delay lives in the <T> element's T attribute, in SAMPLES @48 kHz (real schema:
    # {"PM":..,"P":..,"T":<samples>}); the legacy "D" attribute is accepted for back-compat.
    if ch.delay_raw is not None:
        for key in ("T", "D", "d"):
            if key in ch.delay_raw:
                try:
                    raw = int(float(ch.delay_raw[key]))
                except (TypeError, ValueError):
                    raw = None
                break
    rep.add(f"{label} delay", raw is not None and abs(raw - exp) <= tol.delay_samples,
            exp, raw, f"{delay_ms} ms")


def _check_pct6_eq(ch, specs, label: str, rep: Report, tol: Tolerance) -> None:
    for spec in specs:
        ctrl = f"{label} EQ band {spec.band}"
        if spec.band >= len(ch.eq_bands):
            rep.add(ctrl, False, (spec.f, spec.q, spec.gain_db), "no band")
            continue
        band = ch.eq_bands[spec.band]
        ok = (approx_hz(band.freq, spec.f, tol) and approx(band.q, spec.q, tol.q)
              and approx(band.gain_db, spec.gain_db, tol.gain_db))
        rep.add(ctrl, ok, (round(spec.f, 2), spec.q, spec.gain_db),
                (round(band.freq, 2), round(band.q, 4), round(band.gain_db, 3)))


def _check_pct6_crossover(ch, spec: CrossoverSpec, label: str, rep: Report, tol: Tolerance) -> None:
    from acodsp.channels import _infer_crossover_characteristic

    xo = ch.crossover_bands()
    for kind, corner in (("highpass", spec.hp), ("lowpass", spec.lp)):
        band = xo.get(kind)
        ctrl = f"{label} xover {kind}"
        if corner is None:
            rep.add(ctrl, band is None or band.bypass, "off",
                    "off" if band is None else round(band.freq, 2))
            continue
        if band is None or band.bypass:
            rep.add(ctrl, False, (corner, spec.characteristic, spec.slope), "off")
            continue
        # slope is carried in <Fil G=-slope>; characteristic inferred from the corner-stage Q.
        slope = abs(int(round(band.gain_db)))
        char = _infer_crossover_characteristic(slope, band.q)
        ok = (approx_hz(band.freq, corner, tol) and slope == spec.slope
              and char == spec.characteristic)
        rep.add(ctrl, ok, (corner, spec.characteristic, spec.slope),
                (round(band.freq, 2), char, slope))


def _check_pct6_routing(setup, vector: TestVector, rep: Report, tol: Tolerance) -> None:
    blocks = setup.routing.blocks
    for rc in vector.routes:
        ctrl = f"Route {rc.matrix}[{rc.row},{rc.col}]"
        try:
            border = _ROUTE_ORDER.index(rc.matrix)
        except ValueError:
            rep.add(ctrl, False, rc.gain_db, "unknown matrix")
            continue
        if border >= len(blocks):
            rep.add(ctrl, False, rc.gain_db, "no routing block")
            continue
        block = blocks[border]
        ins = block.ins or 0
        gindex = rc.row * ins + rc.col
        lin = block.gains.get(f"G{gindex}")
        if lin is None or lin <= 0:
            got_db = None
        else:
            got_db = 20.0 * math.log10(lin)
        rep.add(ctrl, got_db is not None and approx(got_db, rc.gain_db, tol.route_db),
                rc.gain_db, None if got_db is None else round(got_db, 3))
