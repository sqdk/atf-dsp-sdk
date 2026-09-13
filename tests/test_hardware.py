"""Opt-in hardware tests.

Enabled only when ACODSP_HW=<port> (e.g. ACODSP_HW=/dev/cu.usbmodem1234) is set;
skipped otherwise so the offline suite stays green with no hardware.

Safety: read-only by default. The write test is further gated by ACODSP_HW_WRITE=1,
and it snapshots the target, writes back the *same* value, then restores — it never
mutes and never leaves the amp on a changed setup. Setup switching uses 0x1F only.
"""
from __future__ import annotations

import os

import pytest

from acodsp.device import Device

PORT = os.environ.get("ACODSP_HW")
ALLOW_WRITE = os.environ.get("ACODSP_HW_WRITE") == "1"

pytestmark = pytest.mark.skipif(not PORT, reason="set ACODSP_HW=<port> to run hardware tests")


@pytest.fixture()
def dev():
    d = Device.connect(port=PORT, verbose=True)
    try:
        yield d
    finally:
        d.close()


def test_identify(dev):
    ident = dev.identify()
    assert ident.raw  # something came back
    assert dev.model


def test_manual_enable_flag_is_zero(dev):
    # No remote engaged -> enable flag 0 (amp not muted).
    flag = dev.manual_enable()
    assert flag in (0, None)


def test_get_and_restore_setup_via_0x1f(dev):
    original = dev.get_setup()
    assert original is not None
    target = 1 if original != 1 else 2
    try:
        dev.set_setup(target)
        assert dev.get_setup() == target
        # enable flag must remain 0 (0x1F path does not engage the remote / mute).
        assert dev.manual_enable() in (0, None)
    finally:
        dev.set_setup(original)
        assert dev.get_setup() == original


def test_read_param(dev):
    # Read a stable readback cell (volume readback @ 4600).
    data = dev.read_param(4600, nbytes=4)
    assert len(data) == 4


@pytest.mark.skipif(not ALLOW_WRITE, reason="set ACODSP_HW_WRITE=1 to allow the write round-trip")
def test_write_roundtrip_restores(dev):
    addr = 4600  # benign readback cell; we write back the same value we read
    snap = dev.snapshot([addr])
    try:
        # Write the exact same bytes back (no audible change) to exercise the path.
        dev.write_param(addr, snap[hex(addr)])
        readback = dev.read_param(addr, nbytes=len(snap[hex(addr)]))
        assert readback == snap[hex(addr)]
    finally:
        dev.restore(snap)
