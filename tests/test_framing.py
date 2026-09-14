"""Offline framing tests: asymmetric LEN, checksum, ~LEN validation, round-trips."""
from __future__ import annotations

import pytest

from atf_dsp.transport import (
    CMD_START,
    RESP_START,
    build_frame,
    checksum,
    parse_frames,
)


def _resp_frame(payload: bytes, type_byte: int = 0x01) -> bytes:
    """Build a device->host RESPONSE frame (LEN = payload_len, no +1)."""
    length = len(payload) & 0xFF
    return bytes([RESP_START, length, (~length) & 0xFF, type_byte]) + payload + bytes([checksum(payload)])


def test_build_frame_matches_confirmed_select_setup_1():
    # setup 1 (idx 0): 42 09 F6 01 1F 00 00 09 00 00 00 00 28
    payload = bytes([0x1F, 0x00, 0x00, 0x09, 0x00, 0x00, 0x00, 0x00])
    frame = build_frame(payload)
    assert frame == bytes.fromhex("4209F6011F0000090000000028")


def test_build_frame_matches_confirmed_get_setup():
    # get current setup: 42 03 FC 01 28 40 68
    frame = build_frame(bytes([0x28, 0x40]))
    assert frame == bytes.fromhex("4203FC01284068")


def test_command_frame_is_asymmetric_len_plus_one():
    payload = bytes([0x28, 0x40])
    frame = build_frame(payload)
    assert frame[0] == CMD_START
    assert frame[1] == len(payload) + 1          # LEN = payload+1 (command asymmetry)
    assert frame[2] == (~frame[1]) & 0xFF         # ~LEN
    assert frame[3] == 0x01                        # fixed type byte
    assert frame[-1] == checksum(payload)


def test_parse_command_frame_roundtrip():
    payload = bytes([0x02, 0x00, 0x11, 0xF8, 0, 0, 0, 0])
    frame = build_frame(payload)
    frames = parse_frames(frame)
    assert len(frames) == 1
    f = frames[0]
    assert f.direction == "TX"
    assert f.checksum_ok
    assert f.payload == payload


def test_parse_response_frame_uses_len_equals_payload():
    # A response with LEN == payload_len (asymmetry: no +1 on this direction).
    payload = bytes([0x28, 0x40, 0x07])   # current setup 7
    frame = _resp_frame(payload)
    frames = parse_frames(frame)
    assert len(frames) == 1
    f = frames[0]
    assert f.direction == "RX"
    assert f.payload == payload
    assert f.checksum_ok


def test_confirmed_response_setup7_wire():
    # RX 43 03 FC 01 28 40 07 6F
    frame = bytes.fromhex("4303FC012840076F")
    frames = parse_frames(frame)
    assert len(frames) == 1
    assert frames[0].payload == bytes([0x28, 0x40, 0x07])


def test_reject_bad_checksum():
    frame = bytearray(build_frame(bytes([0x28, 0x40])))
    frame[-1] ^= 0xFF   # corrupt checksum
    assert parse_frames(bytes(frame)) == []


def test_reject_bad_len_complement():
    frame = bytearray(build_frame(bytes([0x28, 0x40])))
    frame[2] ^= 0xFF   # corrupt ~LEN
    assert parse_frames(bytes(frame)) == []


def test_parse_finds_frame_amid_noise():
    payload = bytes([0x08, 0x01])
    frame = build_frame(payload)
    buf = b"\x00\xff\x13" + frame + b"\x99\x99"
    frames = parse_frames(buf)
    assert len(frames) == 1
    assert frames[0].payload == payload


def test_parse_mixed_directions():
    tx = build_frame(bytes([0x28, 0x40]))
    rx = _resp_frame(bytes([0x28, 0x40, 0x07]))
    frames = parse_frames(tx + rx)
    assert [f.direction for f in frames] == ["TX", "RX"]


def test_checksum_wraps_mod_256():
    assert checksum(bytes([0xFF, 0x02])) == 0x01
