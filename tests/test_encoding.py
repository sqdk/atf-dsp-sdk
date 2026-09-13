"""Offline encoder tests: assert the protocol.yaml sentinels EXACTLY (8.24 fixed
point, RBJ biquad storage order [B2,B1,B0,A2,A1], delay sample math)."""
from __future__ import annotations

import pytest

from acodsp import encoding
from acodsp.encoding import (
    FRAC_BITS,
    UNITY,
    biquad_rbj,
    delay_ms_to_samples,
    from_fixed,
    gain_db_to_raw,
    to_fixed,
)


# --- fixed-point format ------------------------------------------------------
def test_frac_bits_is_8_24_from_contract():
    assert FRAC_BITS == 24
    assert UNITY == 0x01000000


def test_fixed_point_sentinels():
    assert to_fixed(1.0) == 0x01000000   # unity
    assert to_fixed(0.5) == 0x00800000   # half
    assert to_fixed(2.0) == 0x02000000   # 2.0
    assert to_fixed(-1.0) == 0xFF000000  # minus one (two's-complement)


@pytest.mark.parametrize("value", [0.0, 0.5, 1.0, 2.0, -1.0, -0.25, 3.14159, 63.5])
def test_to_from_fixed_roundtrip(value):
    assert from_fixed(to_fixed(value)) == pytest.approx(value, abs=2 ** -FRAC_BITS)


def test_from_fixed_sentinels():
    assert from_fixed(0x01000000) == 1.0
    assert from_fixed(0x00800000) == 0.5
    assert from_fixed(0x02000000) == 2.0
    assert from_fixed(0xFF000000) == -1.0


def test_to_fixed_clamps_to_range():
    assert to_fixed(1000.0) == 0x7FFFFFFF    # saturates high
    assert to_fixed(-1000.0) == 0x80000000   # saturates low


# --- gain (protocol.yaml.encoders.gain examples) -----------------------------
def test_gain_sentinels():
    assert gain_db_to_raw(0) == 0x01000000
    assert gain_db_to_raw(-6) == 0x00804DCE
    assert gain_db_to_raw(6) == 0x01FEC983
    assert gain_db_to_raw(-60) == 0x00004189


# --- biquad (protocol.yaml.encoders.eq_band.example) -------------------------
def test_biquad_peaking_sentinel_order_b2_b1_b0_a2_a1():
    # +6dB peaking, f0=1kHz, Q=1.0, fs=48k -> stored [B2,B1,B0,A2,A1]
    coeffs = biquad_rbj("peaking", 1000, 1.0, 6.0, fs=48000)
    assert coeffs == [0x00DE230C, 0xFE1ACC43, 0x010B4082, 0xFF169C71, 0x01E533BD]


def test_biquad_bypass_is_unity_b0_only():
    # 0dB peaking -> true bypass: B0 unity, all others zero.
    assert biquad_rbj("peaking", 1000, 1.0, 0.0) == [0, 0, 0x01000000, 0, 0]


def test_biquad_named_matches_order():
    named = encoding.biquad_rbj_named("peaking", 1000, 1.0, 6.0, fs=48000)
    assert named == {
        "B2": 0x00DE230C,
        "B1": 0xFE1ACC43,
        "B0": 0x010B4082,
        "A2": 0xFF169C71,
        "A1": 0x01E533BD,
    }


@pytest.mark.parametrize("kind", ["lowpass", "highpass", "lowshelf", "highshelf"])
def test_biquad_other_kinds_return_five_words(kind):
    coeffs = biquad_rbj(kind, 1000, 0.7071, 3.0, fs=48000)
    assert len(coeffs) == 5
    assert all(0 <= c <= 0xFFFFFFFF for c in coeffs)


def test_biquad_unknown_kind_rejected():
    with pytest.raises(ValueError):
        biquad_rbj("bandreject", 1000, 1.0, 0.0)


# --- delay (protocol.yaml.encoders.delay examples_48k) -----------------------
def test_delay_samples_48k():
    assert delay_ms_to_samples(10, fs=48000) == 480      # 0x000001E0
    assert delay_ms_to_samples(100, fs=48000) == 4800    # 0x000012C0
    assert delay_ms_to_samples(0, fs=48000) == 0


def test_delay_negative_rejected():
    with pytest.raises(ValueError):
        delay_ms_to_samples(-1)
