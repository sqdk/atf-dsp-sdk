"""Offline tests for the .pct6/.afpx setup reader/writer (atf_dsp.pct6).

Covers: the crypto/container codec round-trip, parsing the shipped sample into a
structured Setup (metadata, per-channel EQ/gain/mute, band ordering, filter-type
mapping), the save/re-load round-trip (Phase D), and a dry-run apply onto a device's
channel model. All offline — no hardware, no vendor file committed.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from atf_dsp.device import Device
from atf_dsp.params import ParamMap
from atf_dsp.pct6 import (
    CN_NAMES,
    CN_NAMES_UNKNOWN,
    FIL_TYPE_MAP,
    PidMismatchError,
    Setup,
    decrypt,
    encrypt,
    q_compress,
    q_uncompress,
)
from atf_dsp.transport import Link, parse_frames

SAMPLE = (Path(__file__).resolve().parents[2]
          / "extracted/app/setups/UP_8BMW_PP-BMW1_7Hifi_Basic.pct6")

pytestmark = pytest.mark.skipif(not SAMPLE.exists(), reason="vendor sample .pct6 not present")


@pytest.fixture()
def setup() -> Setup:
    return Setup.load(SAMPLE)


# --- crypto / container codec -----------------------------------------------
def test_qcompress_roundtrip():
    payload = b"<ATF>hello</ATF>" * 100
    assert q_uncompress(q_compress(payload)) == payload


def test_codec_roundtrip_is_exact():
    data = SAMPLE.read_bytes()
    variant, xml = decrypt(data)
    assert variant == "XOR:ATFV6"
    assert xml.startswith(b"<ATF ")
    # decrypt(encrypt(xml)) must reproduce the plaintext exactly (lossless zlib + XOR).
    assert decrypt(encrypt(xml, variant))[1] == xml


# --- metadata ---------------------------------------------------------------
def test_metadata(setup: Setup):
    md = setup.metadata
    assert md.pid == 50               # <ATF Dev="50"> — internal device id (NOT the USB PID)
    assert md.version == "6.03.03"
    assert md.outs == 20 and md.ins == 10
    # <OC> splits into virtual (no crossover) + physical outputs (carry HPi/LPi).
    # UP-8BMW: 11 virtual + 9 output = 20 (NOT OUTS/2).
    assert len(setup.virtuals) == 11 and len(setup.outputs) == 9 and len(setup.inputs) == 10
    assert md.filename.endswith("UP_8BMW_PP-BMW1_7Hifi_Basic.pct6")


# --- per-channel parse ------------------------------------------------------
def test_outputs_and_inputs_parse(setup: Setup):
    # Every channel carries a name_code (CN) and an eq_bands list.
    assert all(ch.name_code is not None for ch in (*setup.virtuals, *setup.outputs))
    assert all(ic.name_code is not None for ic in setup.inputs)
    # Virtual channels are 30-band graphic EQ with NO crossover; physical outputs are 32
    # (30 graphic + a low/high-pass crossover pair) and carry HPi/LPi.
    assert len(setup.virtuals[0].eq_bands) == 30 and setup.virtuals[0].hp_index is None
    assert len(setup.outputs[0].eq_bands) == 32 and setup.outputs[0].hp_index is not None


def test_eq_bands_low_to_high_and_graphic_q(setup: Setup):
    bands = setup.virtuals[0].eq_bands
    # low -> high frequency ordering; first band is the 25 Hz graphic band.
    assert bands[0].freq == 25.0
    assert [b.freq for b in bands] == sorted(b.freq for b in bands)
    # Graphic bands use the confirmed Q = 4.3.
    assert bands[0].q == pytest.approx(4.3)
    assert bands[0].kind == "peaking" and bands[0].raw_type == 1


def test_gain_from_level(setup: Setup):
    # <Vol L=...> is a linear level multiplier; L=1 -> 0 dB, L=1.2589 -> +2 dB.
    assert setup.virtuals[0].gain_db == pytest.approx(0.0, abs=1e-9)
    # MT is the ASSIGNED flag, NOT mute: virtuals[0] carries MT=1 and Vol L=1 (0 dB, not
    # muted). mute is derived from the <Vol L> level (<=0 == muted), so it is False here.
    assert setup.virtuals[0].assigned is True
    assert setup.virtuals[0].mute is False
    # a +2 dB level exists among the physical outputs (was flat OC[12]).
    assert any(o.gain_db is not None and o.gain_db == pytest.approx(2.0, abs=1e-3)
               for o in setup.outputs)


def test_mt_is_assigned_not_mute(setup: Setup):
    """MT reflects channel ASSIGNMENT (populated), not mute. Real unassigned outputs
    (CN=0) carry MT=0; assigned ones MT=1. Mute lives in the <Vol L> level."""
    for ch in (*setup.virtuals, *setup.outputs):
        if ch.name_code == 0:
            assert ch.assigned is False        # CN=0 -> unassigned -> MT=0
        else:
            assert ch.assigned is True         # a real role -> assigned -> MT=1
    # inputs carry MT=0 (the assigned flag is an output concept)
    assert all(ic.assigned is False for ic in setup.inputs)


def test_channel_names_from_cn(setup: Setup):
    # Names resolve from the CN role code via the exe-recovered CN_NAMES table.
    # UP-8BMW inputs (validated ground truth): CN 1/2=Front L/R, 14/15=Rear L/R,
    # 26/27=Subwoofer 1/2, 38/39=Digital In L/R, 54/55=AUX In L/R 1.
    by_cn = {ic.name_code: ic.name for ic in setup.inputs}
    assert by_cn.get(1) == "Front Left" and by_cn.get(2) == "Front Right"
    assert by_cn.get(38) == "Digital In Left" and by_cn.get(39) == "Digital In Right"
    # every input resolved to a real name (not None), and matches the table
    for ic in setup.inputs:
        assert ic.name == CN_NAMES.get(ic.name_code)
        assert ic.name is not None
    # codes present in migrated setups but absent from the analyzed map are flagged "Unknown"
    assert CN_NAMES_UNKNOWN == {13, 25}
    assert CN_NAMES[13] == "Unknown (CN13)" and CN_NAMES[25] == "Unknown (CN25)"


def test_crossover_indices_resolve(setup: Setup):
    # A physical output's HPi/LPi point at its highpass/lowpass <Fil> bands.
    oc = setup.outputs[0]
    xo = oc.crossover_bands()
    assert xo["lowpass"] is not None and xo["lowpass"].kind == "lowpass"
    assert xo["highpass"] is not None and xo["highpass"].kind == "highpass"


def test_filter_type_map_and_no_unmapped(setup: Setup):
    # peaking 1/17; crossovers encode characteristic in the code: Butterworth lp=9/hp=10,
    # Linkwitz-Riley lp=15/hp=16 (confirmed vs the real M54 file + PC-Tool).
    assert FIL_TYPE_MAP == {1: "peaking", 17: "peaking", 9: "lowpass", 10: "highpass",
                            15: "lowpass", 16: "highpass"}
    assert setup.unmapped_filter_types() == []


def test_routing_blocks(setup: Setup):
    assert len(setup.routing.blocks) == 5
    b0 = setup.routing.blocks[0]
    assert b0.ins == 10 and b0.outs == 11
    # gains are parsed as linear floats; the block has some non-zero mix cells.
    assert any(v != 0.0 for v in b0.gains.values())


# --- save / re-load round-trip (Phase D) ------------------------------------
def test_save_reload_equal_model(setup: Setup, tmp_path: Path):
    out = tmp_path / "resaved.pct6"
    setup.save(out)
    reloaded = Setup.load(out)
    assert reloaded == setup
    # ...and the re-encrypted file decrypts back to the same XML the model serialises.
    assert decrypt(out.read_bytes())[1] == setup.to_xml()


# --- dry-run apply ----------------------------------------------------------
def _dry_device() -> Device:
    return Device(link=Link(dry_run=True), param_map=ParamMap.load("MatchM54DSP"),
                  model="MATCH M 5.4DSP")


def test_apply_refuses_on_dev_id_mismatch(setup: Setup):
    dev = _dry_device()
    # Sample Dev=50 (UP 8BMW) vs the M 5.4DSP's INTERNAL Dev id 29 -> clean refusal.
    # (Both are PC-Tool internal ids in the SAME number space — NOT the USB PID.)
    with pytest.raises(PidMismatchError):
        dev.apply_setup(setup, dry_run=True)


def test_apply_allows_matching_dev_id():
    """A legitimate M 5.4DSP setup (Dev=29) applies to the M 5.4DSP (internal id 29) —
    this is the case the old USB-PID guard wrongly rejected (29 != USB PID 8200)."""
    dev = _dry_device()
    m54_setup = Setup.from_device(dev)          # from_device now stamps Dev=29
    assert m54_setup.metadata.pid == 29
    res = dev.apply_setup(m54_setup, dry_run=True)   # no force, no PidMismatchError
    assert res.executed and res.pid_setup == 29 and res.pid_device == 29


def test_from_device_emits_dev29_and_six_inputs():
    """from_device on the dry-run M 5.4DSP produces Dev=29 and INS=6 (6 <IC> IN 0..5)."""
    dev = _dry_device()
    s = Setup.from_device(dev)
    assert s.metadata.pid == 29 and s.metadata.ins == 6
    assert len(s.inputs) == 6
    assert [ic.raw.get("IN") for ic in s.inputs] == ["0", "1", "2", "3", "4", "5"]
    # digital inputs (IN 4/5) carry no EQ bands; analog (IN 0..3) do.
    assert s.inputs[4].eq_bands == [] and s.inputs[5].eq_bands == []
    assert len(s.inputs[0].eq_bands) > 0


def test_from_device_dev_ins_survive_save_load(tmp_path):
    """Dev/INS + the IN source indices round-trip through save -> Setup.load."""
    dev = _dry_device()
    s = Setup.from_device(dev)
    out = tmp_path / "m54_from_device.pct6"
    s.save(out)
    reloaded = Setup.load(out)
    assert reloaded.metadata.pid == 29 and reloaded.metadata.ins == 6
    assert len(reloaded.inputs) == 6
    assert [ic.raw.get("IN") for ic in reloaded.inputs] == ["0", "1", "2", "3", "4", "5"]


def test_apply_dry_run_emits_write_frames(setup: Setup):
    dev = _dry_device()
    res = dev.apply_setup(setup, dry_run=True, force=True)
    assert res.executed and res.writes > 0
    tx = [f for frame in dev.link.sent for f in parse_frames(frame) if f.direction == "TX"]
    op3 = [f for f in tx if f.payload[:1] == b"\x03"]
    # gain + EQ writes go through SafeLoad (0x03) — many frames, no hardware.
    assert len(op3) > 0 and len(op3) == res.frames
