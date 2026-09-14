"""Offline tests for the end-to-end validation harness (atf_dsp.validate).

All offline against the MEMORY-BACKED dry-run Link (writes round-trip). Covers both
directions of the PC-Tool-oracle validation:

* Direction B — ``exercise_all`` writes the full deterministic test vector through the atf_dsp
  control setters and reads every cell back within tolerance (proves persistence + encode/decode
  inverse + SafeLoad; PARTIALLY CIRCULAR — same encoders both ends).
* Direction A — ``from_device`` -> ``save`` -> ``assert_matches_pct6`` validates the ``.pct6``
  DECODE against the vector for all DSP-backed fields, including the newly-recovered crossovers.

Plus: the crossover corner/slope recovery in isolation, the tolerance helpers, that a deliberate
mutation is CAUGHT (the harness is not vacuous), and that snapshot/restore leaves the device
clean. Any REAL-device path is guarded behind the existing ``ATF_DSP_SDK_HW`` hardware gate.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from atf_dsp.device import Device
from atf_dsp.params import ParamMap
from atf_dsp.pct6 import Setup
from atf_dsp.transport import Link
from atf_dsp import validate as V


def make_device() -> Device:
    link = Link(dry_run=True, mem={})
    link.pid = 0x2008
    return Device(link=link, param_map=ParamMap.load("MatchM54DSP"), model="MATCH M 5.4DSP")


# --------------------------------------------------------------------------- vector
def test_build_test_vector_is_deterministic_and_distinct():
    m = make_device().model
    v1 = V.build_test_vector(m)
    v2 = V.build_test_vector(m)
    assert v1 == v2                                        # deterministic
    assert len(v1.outputs) == 9 and len(v1.inputs) == 4    # full topology covered
    assert v1.virtuals and v1.routes
    # gains distinct across outputs (a mis-mapped cell would then be caught by value)
    gains = [o.gain_db for o in v1.outputs]
    assert len(set(gains)) == len(gains)
    # some outputs muted, some crossovers HP-only / LP-only (off-section coverage)
    assert any(o.mute for o in v1.outputs) and not all(o.mute for o in v1.outputs)
    assert any(o.crossover.hp is None for o in v1.outputs)
    assert any(o.crossover.lp is None for o in v1.outputs)


# --------------------------------------------------------------------------- Direction B
def test_exercise_all_roundtrips_full_vector_offline():
    dev = make_device()
    rep = V.exercise_all(dev, snapshot=False)              # persist writes
    assert rep.ok, rep.summary()
    assert rep.failed == 0 and rep.passed == len(rep.checks) > 100
    # covers every control family
    controls = " ".join(c.control for c in rep.checks)
    for token in ("gain", "mute", "delay", "EQ band", "xover", "Route"):
        assert token in controls


def test_exercise_all_snapshot_restores_device():
    dev = make_device()
    rep = V.exercise_all(dev, snapshot=True)               # snapshot -> apply -> read -> restore
    assert rep.ok, rep.summary()
    # every cell restored to its pre-apply state (fresh device == all-zero words)
    assert all(word == b"\x00\x00\x00\x00" for word in dev.link.mem.values())


def test_exercise_all_catches_a_mutation():
    """The harness must FAIL when a cell is wrong — otherwise it proves nothing."""
    dev = make_device()
    vec = V.build_test_vector(dev.model)
    # apply, then corrupt Output A's gain cell so the readback disagrees with the vector
    V._apply_vector(dev.model, vec, allow_unsafe=True)
    a_gain = dev.model.output("A").resolve_gain().name
    from atf_dsp import encoding
    dev.link.mem[dev._resolve_addr(a_gain)] = encoding.to_bytes_be(encoding.gain_db_to_raw(+11.0))
    rep = V.Report(direction="mut")
    V._readback_and_check(dev, vec, rep, V.DEFAULT_TOL)
    assert not rep.ok
    assert any("Output A gain" in c.control for c in rep.failures())


# --------------------------------------------------------------------------- Direction A
def test_from_device_roundtrip_matches_pct6(tmp_path: Path):
    dev = make_device()
    vec = V.build_test_vector(dev.model)
    V.exercise_all(dev, vec, snapshot=False)               # persist onto the simulated DSP
    setup = Setup.from_device(dev)
    out = tmp_path / "vector.pct6"
    setup.save(out)                                        # encrypt like the PC-Tool would
    rep = V.assert_matches_pct6(out, vec)
    assert rep.ok, rep.summary()
    # crossovers are among the compared fields now (not skipped)
    assert any("xover" in c.control for c in rep.checks)
    # lossy .pct6 fields are annotated as skipped, never asserted
    assert rep.skipped and any("CN" in s for s in rep.skipped)


def test_assert_matches_pct6_accepts_a_setup_object(tmp_path: Path):
    dev = make_device()
    vec = V.build_test_vector(dev.model)
    V.exercise_all(dev, vec, snapshot=False)
    setup = Setup.from_device(dev)                         # pass the Setup directly (no file)
    assert V.assert_matches_pct6(setup, vec).ok


# --------------------------------------------------------------------------- crossover recovery
@pytest.mark.parametrize("char,slope", [
    ("butterworth", 12), ("butterworth", 24),
    ("linkwitz_riley", 12), ("linkwitz_riley", 24),
])
def test_crossover_recovery_recovers_set_corner_and_slope(char, slope):
    dev = make_device()
    oc = dev.model.output("A")
    oc.crossover(hp=95.0, lp=3200.0, characteristic=char, slope=slope, unsafe=True)
    rec = oc.recover_crossover()
    hp, lp = rec["highpass"], rec["lowpass"]
    assert not hp.off and not lp.off
    assert hp.corner_hz == pytest.approx(95.0, abs=0.5)
    assert lp.corner_hz == pytest.approx(3200.0, rel=0.01)
    assert hp.slope_db == slope and lp.slope_db == slope
    assert hp.characteristic == char and lp.characteristic == char


def test_crossover_off_section_recovers_off():
    dev = make_device()
    oc = dev.model.output("B")
    oc.crossover(hp=120.0, lp=None, characteristic="butterworth", slope=12, unsafe=True)
    rec = oc.recover_crossover()
    assert not rec["highpass"].off and rec["highpass"].corner_hz == pytest.approx(120.0, abs=0.5)
    assert rec["lowpass"].off                              # LP never written -> off


# --------------------------------------------------------------------------- tolerance helpers
def test_tolerance_helpers_behave():
    tol = V.DEFAULT_TOL
    # band = max(±0.5 Hz, ±1%·expected); at 40 Hz the 0.5 Hz floor dominates (1% = 0.4 Hz)
    assert V.approx_hz(40.45, 40.0, tol) and not V.approx_hz(40.6, 40.0, tol)
    # at 10 kHz the 1% (100 Hz) band dominates
    assert V.approx_hz(10050.0, 10000.0, tol)             # within 1% (100 Hz)
    assert not V.approx_hz(10200.0, 10000.0, tol)         # 2% off
    assert V.approx(1.015, 1.0, tol.q) and not V.approx(1.05, 1.0, tol.q)
    assert V.approx(-3.05, -3.0, tol.gain_db) and not V.approx(-3.2, -3.0, tol.gain_db)


def test_report_summary_and_bool():
    rep = V.Report(direction="x")
    rep.add("ctrl-1", True, 1, 1)
    assert rep.ok and bool(rep) and "1/1" in rep.summary()
    rep.add("ctrl-2", False, 2, 3)
    assert not rep.ok and not bool(rep)
    with pytest.raises(AssertionError):
        rep.raise_for_status()


# --------------------------------------------------------------------------- hardware gate
_HW_PORT = os.environ.get("ATF_DSP_SDK_HW")
_HW_WRITE = os.environ.get("ATF_DSP_SDK_HW_WRITE") == "1"


@pytest.mark.skipif(not (_HW_PORT and _HW_WRITE),
                    reason="set ATF_DSP_SDK_HW=<port> and ATF_DSP_SDK_HW_WRITE=1 to run the on-amp harness")
def test_exercise_all_on_real_hardware():
    """On-amp Direction B: snapshot -> write the full vector -> read back -> restore.

    Snapshot/restore wrap the run so the amp is left exactly as found. This is the ONE path the
    offline tests cannot cover semantically — but here it still only proves persistence +
    encode/decode (Direction A against a PC-Tool ``.pct6`` is the independent oracle)."""
    dev = Device.connect(port=_HW_PORT)
    try:
        rep = V.exercise_all(dev, snapshot=True)
        assert rep.ok, rep.summary()
    finally:
        dev.close()
