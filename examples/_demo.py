"""Shared helper for the examples — a Device you can run WITHOUT hardware.

By default this returns a **memory-backed simulated MATCH M 5.4DSP**: writes update an
in-memory store and reads return it, so every example round-trips offline (no amp needed).
Set ``ATF_DSP_SDK_HW=<port>`` to run the exact same script against a real amplifier instead.
"""
from __future__ import annotations

import os

from atf_dsp.device import Device
from atf_dsp.params import ParamMap
from atf_dsp.transport import Link

MODEL = "MATCH M 5.4DSP"
BASENAME = "MatchM54DSP"


def demo_device() -> Device:
    """A real Device if ATF_DSP_SDK_HW is set, else a memory-backed simulated one."""
    port = os.environ.get("ATF_DSP_SDK_HW")
    if port:
        return Device.connect(port=port, model=MODEL, verbose=False)
    # Memory-backed dry-run: 0x03 writes update `mem`, 0x02 reads return it → round-trips offline.
    return Device(link=Link(dry_run=True, mem={}), param_map=ParamMap.load(BASENAME), model=MODEL)
