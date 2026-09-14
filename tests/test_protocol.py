"""Offline opcode-encoder tests: select_setup, read/write headers, big-endian addr."""
from __future__ import annotations

import pytest

from atf_dsp import protocol
from atf_dsp.protocol import (
    encode_get,
    encode_identify,
    encode_read_param,
    encode_select_setup,
    encode_write_param,
)
from atf_dsp.transport import build_frame


def test_select_setup_payload_bytes():
    # 1F 00 00 09 00 <idx> 00 00
    assert encode_select_setup(0) == bytes([0x1F, 0x00, 0x00, 0x09, 0x00, 0x00, 0x00, 0x00])
    assert encode_select_setup(6) == bytes([0x1F, 0x00, 0x00, 0x09, 0x00, 0x06, 0x00, 0x00])


def test_select_setup_full_frame_matches_confirmed_wire():
    # setup 1 (idx 0): 42 09 F6 01 1F 00 00 09 00 00 00 00 28
    assert build_frame(encode_select_setup(0)) == bytes.fromhex("4209F6011F0000090000000028")


def test_identify_payload():
    assert encode_identify() == bytes([0x08, 0x01])


def test_get_payload_and_confirmed_frame():
    assert encode_get(protocol.GET_CURRENT_SETUP) == bytes([0x28, 0x40])
    assert build_frame(encode_get(0x40)) == bytes.fromhex("4203FC01284068")
    assert encode_get(0x50, b"\x01") == bytes([0x28, 0x50, 0x01])


def test_read_param_header_and_bigendian_addr():
    # 02 <inst> <addr_hi> <addr_lo> + N pad
    p = encode_read_param(0, 0x11F8, nbytes=4)
    assert p[0] == 0x02
    assert p[1] == 0x00                 # instance 0
    assert p[2] == 0x11 and p[3] == 0xF8  # big-endian address
    assert p[4:] == b"\x00\x00\x00\x00"   # N trailing pad bytes set the read size


def test_read_param_addr_1234():
    p = encode_read_param(0, 1234, nbytes=2)
    assert p[2] == (1234 >> 8) & 0xFF
    assert p[3] == 1234 & 0xFF
    assert len(p) == 4 + 2


def test_write_param_direct_flag_and_header():
    # direct: 03 <inst> FF <addr_hi> <addr_lo> <data...>
    data = bytes([0x00, 0x40, 0x00, 0x00])
    p = encode_write_param(0, 0x11F8, data, safeload=False)
    assert p[0] == 0x03
    assert p[1] == 0x00
    assert p[2] == 0xFF                 # 0xFF forces direct
    assert p[3] == 0x11 and p[4] == 0xF8  # big-endian address
    assert p[5:] == data                # data big-endian, verbatim


def test_write_param_safeload_flag():
    data = bytes([0x00, 0x40, 0x00, 0x00])
    p = encode_write_param(0, 0x11F8, data, safeload=True)
    assert p[2] != 0xFF                 # non-0xFF selects SafeLoad
    assert p[2] == protocol.WRITE_FLAG_SAFELOAD


def test_write_default_instance_zero():
    p = encode_write_param(protocol.DEFAULT_INSTANCE, 0x0100, b"\x00\x00\x00\x01")
    assert p[1] == 0x00


def test_addr_out_of_range_rejected():
    with pytest.raises(ValueError):
        encode_read_param(0, 0x1_0000)
    with pytest.raises(ValueError):
        encode_select_setup(300)
