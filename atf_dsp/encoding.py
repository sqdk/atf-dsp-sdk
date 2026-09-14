"""encoding.py — pure SigmaDSP (SIGMA300 / ADAU145x) value encoders.

These functions are the value math only: no transport, no I/O. Everything is
parameterised by the fixed-point format from ``protocol.yaml`` (encoders._fixed_point,
confirmed 8.24 — 24 fractional bits, unity 0x01000000, signed two's-complement,
big-endian on the wire) rather than hard-coded magic numbers.

Coefficient math is the RBJ Audio-EQ cookbook, reimplemented from first principles
(Robert Bristow-Johnson, public domain) so this file carries no LGPL obligation from
MCUdude/SigmaDSP. The on-chip biquad storage order and the a1/a2 sign inversion are
per protocol.yaml.encoders.eq_band (SigmaStudio stores b2,b1,b0,-a2,-a1 / a0=1).
"""
from __future__ import annotations

import math
from typing import Dict, List, Tuple

from atf_dsp.contract import fixed_point

# --- fixed-point format (from protocol.yaml.encoders._fixed_point) ----------
_FP = fixed_point()
FRAC_BITS: int = int(_FP["frac_bits"])                 # 24 (8.24)
UNITY: int = int(_FP["unity"])                         # 0x01000000
_CLAMP_MIN, _CLAMP_MAX = (float(x) for x in _FP["clamp"])  # -128.0 .. 127.99999994

DEFAULT_FS = 48000

_INT32_MIN = -(1 << 31)
_INT32_MAX = (1 << 31) - 1

# EQ band on-chip storage order (protocol.yaml.encoders.eq_band.storage_order).
EQ_STORAGE_ORDER: Tuple[str, str, str, str, str] = ("B2", "B1", "B0", "A2", "A1")

BIQUAD_KINDS = ("peaking", "lowshelf", "highshelf", "lowpass", "highpass")


# --- scalar fixed-point ------------------------------------------------------
def to_fixed(value: float, frac_bits: int = FRAC_BITS) -> int:
    """Encode *value* to a signed fixed-point raw word (default 8.24), clamped.

    Clamps to the format's representable range (±128 for 8.24) then rounds and
    returns the 32-bit two's-complement word as an unsigned int (0..0xFFFFFFFF).
    """
    clamped = max(_CLAMP_MIN, min(_CLAMP_MAX, value))
    raw = round(clamped * (1 << frac_bits))
    raw = max(_INT32_MIN, min(_INT32_MAX, raw))
    return raw & 0xFFFFFFFF


def from_fixed(raw: int, frac_bits: int = FRAC_BITS) -> float:
    """Decode a fixed-point raw word (two's-complement 32-bit) back to a float."""
    raw &= 0xFFFFFFFF
    if raw & 0x80000000:
        raw -= 1 << 32
    return raw / (1 << frac_bits)


def to_bytes_be(raw: int) -> bytes:
    """Serialise a 32-bit raw word big-endian (matches dsp_rw.byte_order)."""
    return (raw & 0xFFFFFFFF).to_bytes(4, "big")


def gain_db_to_raw(db: float) -> int:
    """Convert a gain in dB to an 8.24 linear-multiplier raw word.

    raw = to_fixed(10**(dB/20)). Sentinels (protocol.yaml.encoders.gain):
    0dB->0x01000000, -6dB->0x00804DCE, +6dB->0x01FEC983, -60dB->0x00004189.
    """
    return to_fixed(10 ** (db / 20.0))


# --- delay -------------------------------------------------------------------
def delay_ms_to_samples(ms: float, fs: int = DEFAULT_FS) -> int:
    """Convert a delay in milliseconds to an integer sample count (32.0 format).

    samples = round(ms/1000 * fs). At 48k: 10ms->480 (0x1E0), 100ms->4800.
    (protocol.yaml.encoders.delay — status: hypothesis, exact cell semantics TBD.)
    """
    if ms < 0:
        raise ValueError("delay must be >= 0 ms")
    return int(round(ms / 1000.0 * fs))


# --- biquad (RBJ cookbook) ---------------------------------------------------
def _rbj_coeffs(kind: str, f0: float, q: float, gain_db: float, fs: int) -> Tuple[float, ...]:
    """Return raw (b0,b1,b2,a0,a1,a2) for an RBJ biquad (unnormalised by a0)."""
    if q <= 0:
        raise ValueError("Q must be > 0")
    if not 0 < f0 < fs / 2:
        raise ValueError(f"f0 must be in (0, fs/2); got {f0} with fs={fs}")

    w0 = 2 * math.pi * f0 / fs
    cos_w0 = math.cos(w0)
    sin_w0 = math.sin(w0)
    alpha = sin_w0 / (2 * q)
    a_amp = 10 ** (gain_db / 40.0)   # sqrt of linear gain, per RBJ shelving/peaking

    if kind == "peaking":
        b0 = 1 + alpha * a_amp
        b1 = -2 * cos_w0
        b2 = 1 - alpha * a_amp
        a0 = 1 + alpha / a_amp
        a1 = -2 * cos_w0
        a2 = 1 - alpha / a_amp
    elif kind == "lowpass":
        b0 = (1 - cos_w0) / 2
        b1 = 1 - cos_w0
        b2 = (1 - cos_w0) / 2
        a0 = 1 + alpha
        a1 = -2 * cos_w0
        a2 = 1 - alpha
    elif kind == "highpass":
        b0 = (1 + cos_w0) / 2
        b1 = -(1 + cos_w0)
        b2 = (1 + cos_w0) / 2
        a0 = 1 + alpha
        a1 = -2 * cos_w0
        a2 = 1 - alpha
    elif kind == "lowshelf":
        two_sqrt_a_alpha = 2 * math.sqrt(a_amp) * alpha
        b0 = a_amp * ((a_amp + 1) - (a_amp - 1) * cos_w0 + two_sqrt_a_alpha)
        b1 = 2 * a_amp * ((a_amp - 1) - (a_amp + 1) * cos_w0)
        b2 = a_amp * ((a_amp + 1) - (a_amp - 1) * cos_w0 - two_sqrt_a_alpha)
        a0 = (a_amp + 1) + (a_amp - 1) * cos_w0 + two_sqrt_a_alpha
        a1 = -2 * ((a_amp - 1) + (a_amp + 1) * cos_w0)
        a2 = (a_amp + 1) + (a_amp - 1) * cos_w0 - two_sqrt_a_alpha
    elif kind == "highshelf":
        two_sqrt_a_alpha = 2 * math.sqrt(a_amp) * alpha
        b0 = a_amp * ((a_amp + 1) + (a_amp - 1) * cos_w0 + two_sqrt_a_alpha)
        b1 = -2 * a_amp * ((a_amp - 1) + (a_amp + 1) * cos_w0)
        b2 = a_amp * ((a_amp + 1) + (a_amp - 1) * cos_w0 - two_sqrt_a_alpha)
        a0 = (a_amp + 1) - (a_amp - 1) * cos_w0 + two_sqrt_a_alpha
        a1 = 2 * ((a_amp - 1) - (a_amp + 1) * cos_w0)
        a2 = (a_amp + 1) - (a_amp - 1) * cos_w0 - two_sqrt_a_alpha
    else:
        raise ValueError(f"unknown biquad kind: {kind!r}; expected one of {BIQUAD_KINDS}")

    return b0, b1, b2, a0, a1, a2


def biquad_rbj(
    kind: str,
    f0: float,
    q: float,
    gain_db: float = 0.0,
    fs: int = DEFAULT_FS,
) -> List[int]:
    """Compute the 5 stored biquad coefficients in on-chip order [B2,B1,B0,A2,A1].

    Normalised by a0 with the SigmaStudio sign convention (store -a1/a0, -a2/a0):
        [b2/a0, b1/a0, b0/a0, -a2/a0, -a1/a0]  as 8.24 raw words.

    A 0 dB peaking band is stored as a true bypass [0,0,unity,0,0] — matching the
    AF1 default image, where a disabled EQ band is a unity B0 with zero siblings
    (protocol.yaml.encoders.eq_band / _fixed_point evidence).
    """
    if kind == "peaking" and gain_db == 0.0:
        return [0, 0, UNITY, 0, 0]

    b0, b1, b2, a0, a1, a2 = _rbj_coeffs(kind, f0, q, gain_db, fs)
    stored = [b2 / a0, b1 / a0, b0 / a0, -a2 / a0, -a1 / a0]
    return [to_fixed(c) for c in stored]


def peaking_from_coeffs(stored: List[int], fs: int = DEFAULT_FS):
    """Invert a stored peaking biquad ([B2,B1,B0,A2,A1] 8.24 words) back to (f0, Q, gain_db).

    The exact analytic inverse of :func:`biquad_rbj` for ``kind='peaking'`` (used by
    :meth:`Setup.from_device` to recover EQ bands read off the DSP). Returns ``None`` for a
    bypassed / uninitialised band (the [0,0,unity,0,0] bypass image, all-zero, or any
    degenerate coeff set) — those carry no meaningful f0/Q.
    """
    if list(stored) == [0, 0, UNITY, 0, 0]:
        return None
    s0, s1, s2, s3, s4 = (from_fixed(w) for w in stored)
    ssum = s2 + s0
    if abs(ssum) < 1e-9:
        return None
    a0 = 2.0 / ssum
    alpha_a = (s2 - s0) / ssum      # alpha * A
    alpha_over_a = a0 - 1.0         # alpha / A
    if alpha_a <= 0 or alpha_over_a <= 0:
        return None
    a_amp = math.sqrt(alpha_a / alpha_over_a)
    alpha = math.sqrt(alpha_a * alpha_over_a)
    cos_w0 = max(-1.0, min(1.0, s4 * a0 / 2.0))
    w0 = math.acos(cos_w0)
    sin_w0 = math.sin(w0)
    if sin_w0 <= 0 or a_amp <= 0:
        return None
    f0 = w0 * fs / (2 * math.pi)
    q = sin_w0 / (2 * alpha)
    gain_db = 40.0 * math.log10(a_amp)
    return (f0, q, gain_db)


def lphp_corner_from_coeffs(stored: List[int], fs: int = DEFAULT_FS):
    """Invert a stored RBJ low/high-pass biquad ([B2,B1,B0,A2,A1] 8.24 words) -> (f0, Q).

    Used by the crossover-corner recovery (:meth:`atf_dsp.channels.OutputChannel.recover_crossover`
    and :meth:`Setup.from_device`) to read a corner frequency + Q back off the DSP's FILTERS
    stages. The RBJ lowpass and highpass share IDENTICAL a-side (pole) coefficients, so f0/Q
    recover the same way for both — WHICH one it is (HP vs LP) is known from the FILTERS stage
    slot (HP=[0,1,2], LP=[3,4,5]), not from the coefficients. The recovery is the exact analytic
    inverse of :func:`biquad_rbj` for ``kind in {'lowpass','highpass'}`` and is used only on the
    DSP-BACKED coefficients we ourselves wrote, so corner/Q come back to full float precision
    (subject to 8.24 rounding).

    Returns ``None`` for a bypassed / all-zero / uninitialised stage (the [0,0,unity,0,0] bypass
    image or an all-zero cell carry no meaningful corner), so callers can count active stages ->
    slope (1 active stage = 12 dB/oct, 2 = 24).
    """
    words = list(stored)
    if words == [0, 0, UNITY, 0, 0] or not any(words):
        return None
    s0, s1, s2, s3, s4 = (from_fixed(w) for w in words)
    denom = 1.0 - s3                      # s3 = -a2/a0 ; a0=1+alpha, a2=1-alpha -> a0(1-s3)=2
    if abs(denom) < 1e-9:
        return None
    a0 = 2.0 / denom
    alpha = (1.0 + s3) / denom            # (a0 - a2)/... = alpha
    if alpha <= 0:
        return None
    cos_w0 = max(-1.0, min(1.0, s4 / denom))   # s4 = -a1/a0 = 2 cos_w0 / a0
    w0 = math.acos(cos_w0)
    sin_w0 = math.sin(w0)
    if sin_w0 <= 0:
        return None
    f0 = w0 * fs / (2 * math.pi)
    q = sin_w0 / (2 * alpha)
    if not (0 < f0 < fs / 2):
        return None
    return (f0, q)


def _biquad_gain(stored: List[int], z: complex) -> complex:
    """Evaluate the transfer function H(z) of a stored [b2,b1,b0,-a2,-a1] biquad."""
    b2, b1, b0, na2, na1 = (from_fixed(w) for w in stored)   # -a2/a0, -a1/a0 in slots 3,4
    zi = 1.0 / z
    num = b0 + b1 * zi + b2 * zi * zi
    den = 1.0 + (-na1) * zi + (-na2) * zi * zi
    return num / den


def shelf_from_coeffs(stored: List[int], fs: int = DEFAULT_FS):
    """Invert a stored RBJ low/high-SHELF biquad -> (kind, f0, Q, gain_db), or ``None`` if the
    band is not a shelf (peaking/flat). Analytic inverse of :func:`biquad_rbj` for the shelf
    kinds. Classification uses the DC and Nyquist gains: a low-shelf lifts/cuts DC (H(1)=A^2)
    with a flat Nyquist; a high-shelf the reverse. Hardware-validated 2026-08-30 (M 5.4DSP).
    """
    if list(stored) == [0, 0, UNITY, 0, 0] or not any(stored):
        return None
    h_dc = _biquad_gain(stored, complex(1.0, 0.0)).real
    h_ny = _biquad_gain(stored, complex(-1.0, 0.0)).real
    if abs(h_dc - 1.0) < 1e-3 and abs(h_ny - 1.0) < 1e-3:
        return None                                    # flat/peaking, not a shelf
    low = abs(h_dc - 1.0) >= abs(h_ny - 1.0)
    gain_lin = h_dc if low else h_ny
    if gain_lin <= 0:
        return None
    gain_db = 20.0 * math.log10(gain_lin)
    a = 10.0 ** (gain_db / 40.0)                        # a_amp
    s0, s1, s2, s3, s4 = (from_fixed(w) for w in stored)
    # a-side (poles): a1/a0 = -s4, a2/a0 = -s3. Low- and high-shelf denominators differ in the
    # sign of the (A-1)cw term; solve cos_w0 from the pole ratio, then a0 and alpha.
    if low:
        rr = (1.0 - s3) / s4 if s4 else 0.0             # [(A+1)+(A-1)cw]/[(A-1)+(A+1)cw]
        cw = ((a + 1) - rr * (a - 1)) / (rr * (a + 1) - (a - 1))
        cw = max(-1.0, min(1.0, cw))
        w0 = math.acos(cw)
        d = 2.0 * ((a + 1) + (a - 1) * cw) / (1.0 - s3)
    else:
        rr = (1.0 - s3) / (-s4) if s4 else 0.0          # [(A+1)-(A-1)cw]/[(A-1)-(A+1)cw]
        cw = ((a + 1) - rr * (a - 1)) / ((a - 1) - rr * (a + 1))
        cw = max(-1.0, min(1.0, cw))
        w0 = math.acos(cw)
        d = 2.0 * ((a + 1) - (a - 1) * cw) / (1.0 - s3)
    alpha = d * (1.0 + s3) / (4.0 * math.sqrt(a))
    sin_w0 = math.sin(w0)
    if alpha <= 0 or sin_w0 <= 0:
        return None
    q = sin_w0 / (2.0 * alpha)
    f0 = w0 * fs / (2.0 * math.pi)
    if not (0 < f0 < fs / 2):
        return None
    return ("lowshelf" if low else "highshelf", f0, q, gain_db)


def eq_band_from_coeffs(stored: List[int], fs: int = DEFAULT_FS):
    """Classify + invert a stored EQ biquad -> ``(kind, f0, Q, gain_db)`` where kind is
    'peaking' | 'lowshelf' | 'highshelf', or ``None`` for a bypassed band. Tries peaking first
    (flat DC+Nyquist), then shelves. Fixes the from_device/dsp_web bug where a shelf was
    force-fit as a wrong peaking band (hardware-confirmed 2026-08-30)."""
    if list(stored) == [0, 0, UNITY, 0, 0] or not any(stored):
        return None
    # Try BOTH inverses and pick the kind whose re-encode best matches the stored coeffs. This
    # disambiguates a low-frequency PEAKING band (whose skirt bends the DC gain, so the DC/Nyquist
    # heuristic alone would mis-call it a shelf) from a true shelf.
    candidates = []
    pk = peaking_from_coeffs(stored, fs)
    if pk is not None:
        candidates.append(("peaking", pk[0], pk[1], pk[2]))
    sh = shelf_from_coeffs(stored, fs)
    if sh is not None:
        candidates.append(sh)                       # (kind, f, q, gain_db)
    best = None
    for kind, f, q, g in candidates:
        try:
            reenc = biquad_rbj(kind, f, q, g, fs)
        except (ValueError, ZeroDivisionError):
            continue
        err = sum(abs(a - b) for a, b in zip(reenc, stored))
        if best is None or err < best[0]:
            best = (err, kind, f, q, g)
    return None if best is None else (best[1], best[2], best[3], best[4])


def is_allpass(stored: List[int], tol: float = 1e-3) -> bool:
    """True when the stored biquad is an all-pass (numerator = reversed denominator): b0=a2,
    b1=a1, b2=a0. Phase-shift stages are all-pass biquads (FILTERS stages 6-8)."""
    if list(stored) == [0, 0, UNITY, 0, 0] or not any(stored):
        return False
    b2, b1, b0, na2, na1 = (from_fixed(w) for w in stored)
    return abs(b2 - 1.0) < tol and abs(b0 - (-na2)) < tol and abs(b1 - (-na1)) < tol


def allpass_phase_deg(stored: List[int], f: float, fs: int = DEFAULT_FS) -> float:
    """Continuous (unwrapped) phase in degrees of a stored 2nd-order all-pass biquad at *f*,
    in the range [-360, 0]. The exact phase is ``atan2(H)`` (wraps at +/-180); the monotonic
    analytic form ``-2w - 2*arg(D)`` picks the right 360-deg branch, so the result is exact AND
    doesn't wrap through -180 deg (needed for the phase design bisection)."""
    w = 2 * math.pi * f / fs
    z = complex(math.cos(w), math.sin(w))
    h = _biquad_gain(stored, z)
    exact = math.degrees(math.atan2(h.imag, h.real))         # precise, in (-180, 180]
    na1, na2 = from_fixed(stored[4]), from_fixed(stored[3])   # -a1/a0, -a2/a0
    re = 1.0 + (-na1) * math.cos(w) + (-na2) * math.cos(2 * w)
    im = na1 * math.sin(w) + na2 * math.sin(2 * w)
    approx = math.degrees(-2.0 * w - 2.0 * math.atan2(im, re))   # monotonic branch guide
    return exact + 360.0 * round((approx - exact) / 360.0)


# The PC-Tool "Phase" control is a 2nd-order all-pass with ~fixed Q whose centre is placed to
# deliver the target phase at the channel's crossover frequency (hardware-derived 2026-08-30:
# 45/90/180 deg -> centres 187/112/69 Hz, Q ~0.86, at fc=80 Hz).
ALLPASS_PHASE_Q = 0.86


def allpass_biquad(phase_deg: float, f_ref: float, fs: int = DEFAULT_FS,
                   q: float = ALLPASS_PHASE_Q) -> List[int]:
    """Design the stored all-pass biquad that adds ``-phase_deg`` of phase at ``f_ref`` (the
    crossover frequency), matching the PC-Tool "Phase" control. Solves the all-pass centre by
    bisection for the given fixed *q*; returns [b2,b1,b0,-a2,-a1] 8.24 words (bypass image at 0)."""
    if phase_deg == 0:
        return [0, 0, UNITY, 0, 0]
    target = -abs(phase_deg)

    def ap(f0: float) -> List[int]:
        w0 = 2 * math.pi * f0 / fs
        cw, alpha = math.cos(w0), math.sin(w0) / (2 * q)
        b0, b1, b2 = 1 - alpha, -2 * cw, 1 + alpha
        a0, a1, a2 = 1 + alpha, -2 * cw, 1 - alpha
        return [to_fixed(c) for c in (b2 / a0, b1 / a0, b0 / a0, -a2 / a0, -a1 / a0)]

    lo, hi = 1.0, fs / 2.0 - 1.0                 # centre freq search range
    for _ in range(60):
        mid = math.sqrt(lo * hi)                 # log-bisection
        ph = allpass_phase_deg(ap(mid), f_ref, fs)
        # phase at f_ref is monotonic in the centre f0 (higher centre -> less lag)
        if ph > target:
            hi = mid
        else:
            lo = mid
    return ap(math.sqrt(lo * hi))


def biquad_rbj_named(
    kind: str,
    f0: float,
    q: float,
    gain_db: float = 0.0,
    fs: int = DEFAULT_FS,
) -> Dict[str, int]:
    """Same as biquad_rbj but keyed by coefficient name (B2,B1,B0,A2,A1)."""
    return dict(zip(EQ_STORAGE_ORDER, biquad_rbj(kind, f0, q, gain_db, fs)))
