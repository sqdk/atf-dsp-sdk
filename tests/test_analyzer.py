"""Offline tests for the PROVISIONAL Input Signal Analyzer (ISA) reader (atf_dsp.analyzer).

No hardware: a FakeDevice returns canned readback bytes and records writes. These lock the
decode math, the swept-read orchestration, the safety gate, and the .at01 address bindings.
Correctness on real hardware is still unproven (see docs/input-analyzer.md).
"""
from __future__ import annotations

import math

import pytest

from atf_dsp import analyzer
from atf_dsp.analyzer import (
    InputAnalyzer,
    Spectrum,
    bandpass_coeffs,
    linear_to_dbfs,
    log_sweep,
    read_input,
    settle_ms,
)
from atf_dsp.encoding import from_fixed, to_bytes_be, to_fixed
from atf_dsp.params import ParamMap

UNITY = 0x01000000
HALF = 0x00800000  # 0.5 in 8.24 → -6.02 dBFS


class FakeDevice:
    """Minimal Device stand-in keyed by param NAME (what analyzer passes)."""

    def __init__(self, reads=None, fs=48000):
        # reads: {name: bytes}  (constant per name)
        self.reads = dict(reads or {})
        self.writes = []  # list of (name, bytes, safeload)
        self.fs = fs      # analyzer defaults its bandpass fs to the device rate

    def read_param(self, name, nbytes=4):
        return self.reads.get(name, b"\x00\x00\x00\x00")[:nbytes]

    def write_param(self, name, data, safeload=True):
        self.writes.append((name, bytes(data), safeload))
        return b""


# ---------------------------------------------------------------- pure helpers
def test_linear_to_dbfs():
    assert linear_to_dbfs(1.0) == pytest.approx(0.0)
    assert linear_to_dbfs(0.5) == pytest.approx(-6.0206, abs=1e-3)
    assert linear_to_dbfs(0.0) == analyzer.DBFS_FLOOR


def test_log_sweep_endpoints_and_monotonic():
    fs = log_sweep(20.0, 20000.0, 61)
    assert len(fs) == 61
    assert fs[0] == pytest.approx(20.0)
    assert fs[-1] == pytest.approx(20000.0)
    assert all(b > a for a, b in zip(fs, fs[1:]))  # strictly increasing


def test_bandpass_coeffs_shape():
    c = bandpass_coeffs(1000.0, q=4.0, fs=48000)
    assert len(c) == 5  # [B2,B1,B0,A2,A1]
    # RBJ BPF has b1 == 0 → stored B1 (index 1) is exactly 0.
    assert c[1] == 0
    # Coefficients decode to sane magnitudes (< a few units in 8.24).
    assert all(abs(from_fixed(w)) < 4.0 for w in c)


def test_settle_ms_shape():
    assert settle_ms(20000.0) == pytest.approx(2.0)          # floored at high freq
    assert settle_ms(20.0) > settle_ms(20000.0)              # longer at low freq
    assert settle_ms(100.0) == pytest.approx(60.0)           # 6 cycles / 100 Hz = 60 ms


# ---------------------------------------------------------------- read-only probe
def test_read_level_decodes_824():
    dev = FakeDevice({analyzer.RBINPUTRTA1: to_bytes_be(HALF)})
    an = InputAnalyzer(dev)
    assert an.read_level_linear() == pytest.approx(0.5)
    assert an.read_level() == pytest.approx(-6.0206, abs=1e-3)
    assert not dev.writes  # side-effect-free


def test_read_input_default_is_readonly_dict():
    dev = FakeDevice({
        analyzer.RBINPUTRTA1: to_bytes_be(UNITY),        # 0 dBFS
        analyzer.INPUT_LEVEL_PEAK: to_bytes_be(HALF),    # -6 dBFS
    })
    out = read_input(dev, input_channel=0)
    assert out["analyzer_rms_dbfs"] == pytest.approx(0.0)
    assert out["input_peak_dbfs"] == pytest.approx(-6.0206, abs=1e-3)
    assert not dev.writes


# ---------------------------------------------------------------- writes un-gated
def test_writes_are_ungated():
    # Promoted out of provisional (2026-08-29): the analyzer write paths execute without an
    # unsafe opt-in and no longer raise. They still write via SafeLoad to the probe cells.
    dev = FakeDevice({analyzer.RBINPUTRTA1: to_bytes_be(HALF)})
    an = InputAnalyzer(dev)
    an.tune(1000.0)
    an.select_input([0])
    an.sweep([1000.0], sleep=lambda s: None)
    assert any(w[0] == analyzer.FILTER1_TARGB2_BASE for w in dev.writes)      # tune/sweep retune
    assert any(w[0] in analyzer.NXMINPUTRTA_VOL for w in dev.writes)          # tap select
    assert all(w[2] is True for w in dev.writes)                             # all SafeLoad


# ---------------------------------------------------------------- swept spectrum
def test_sweep_orchestration():
    dev = FakeDevice({analyzer.RBINPUTRTA1: to_bytes_be(HALF)})
    an = InputAnalyzer(dev)
    freqs = [100.0, 1000.0, 10000.0]
    spec = an.sweep(freqs, sleep=lambda s: None)  # no real waiting
    assert isinstance(spec, Spectrum)
    assert spec.freqs == freqs
    assert len(spec) == 3
    assert all(v == pytest.approx(-6.0206, abs=1e-3) for v in spec.levels_dbfs)
    # One 5-word filter retune (SafeLoad) per point, all to the INPUTRTAFILTER1 base.
    filt_writes = [w for w in dev.writes if w[0] == analyzer.FILTER1_TARGB2_BASE]
    assert len(filt_writes) == 3
    for _, data, safeload in filt_writes:
        assert len(data) == 20 and safeload is True  # 5 × 32-bit words, SafeLoad


def test_sweep_via_read_input_selects_tap():
    dev = FakeDevice({analyzer.RBINPUTRTA1: to_bytes_be(0)})  # silence
    spec = read_input(dev, input_channel=1, sweep=True,
                      freqs=[1000.0], sleep=lambda s: None)
    assert isinstance(spec, Spectrum)
    assert spec.input_channel == 1
    assert spec.levels_dbfs[0] == analyzer.DBFS_FLOOR  # silent cell → floor
    # select_input wrote the 4 NXMINPUTRTA VOL cells (tap 1 = unity, others 0).
    vol_writes = {w[0]: w[1] for w in dev.writes if w[0] in analyzer.NXMINPUTRTA_VOL}
    assert len(vol_writes) == 4
    assert vol_writes[analyzer.NXMINPUTRTA_VOL[1]] == to_bytes_be(UNITY)
    assert vol_writes[analyzer.NXMINPUTRTA_VOL[0]] == to_bytes_be(0)


def test_spectrum_peak():
    spec = Spectrum(freqs=[100.0, 1000.0, 10000.0], levels_dbfs=[-40.0, -3.0, -50.0])
    f, lvl = spec.peak()
    assert f == 1000.0 and lvl == -3.0


# ---------------------------------------------------------------- address bindings
def test_analyzer_param_names_resolve_on_m54dsp():
    """The names analyzer.py uses must map to the RE'd .at01 addresses (findings doc §1)."""
    pm = ParamMap.load("MatchM54DSP")
    assert pm.get(analyzer.RBINPUTRTA1) == 4957
    assert pm.get(analyzer.FILTER1_TARGB2_BASE) == 39450
    assert [pm.get(n) for n in analyzer.NXMINPUTRTA_VOL] == [4940, 4941, 4942, 4943]
    assert pm.get(analyzer.INPUT_LEVEL_PEAK) == 4944
    assert pm.get(analyzer.INPUT_LEVEL_SELECT) == 8541
