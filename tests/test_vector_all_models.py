"""The validation test vector must build for EVERY shipped model, not just the one with a
channel overlay.

Regression for issue #2: on a MATCH UP 4DSP (no `.channels.yaml` overlay) `validate
--print-vector` / `--pct6` crashed because the input-EQ test frequencies were borrowed from
Output A's graphic-EQ table, which only the overlay defines. Models without an overlay now fall
back to the standard ISO 1/3-octave grid.
"""
from __future__ import annotations

import pytest

from atf_dsp import validate
from atf_dsp.device import Device
from atf_dsp.models import MODEL_AT01
from atf_dsp.params import ParamMap
from atf_dsp.transport import Link


def _device(model: str) -> Device:
    return Device(link=Link(dry_run=True, mem={}), param_map=ParamMap.load(MODEL_AT01[model]),
                  model=model)


@pytest.mark.parametrize("model", sorted(MODEL_AT01))
def test_vector_builds_for_every_model(model: str):
    vec = validate.build_test_vector(_device(model).model)
    assert "Test vector" in validate.format_test_vector(vec)


def test_up4dsp_without_overlay_uses_iso_fallback():
    vec = validate.build_test_vector(_device("MATCH UP 4DSP").model)
    assert [o.letter for o in vec.outputs] == ["A", "B", "C", "D", "E"]
    freqs = {b.f for i in vec.inputs for b in i.eq}
    assert freqs and freqs <= set(map(float, validate._ISO_THIRD_OCTAVE_HZ))


def test_overlay_model_keeps_its_own_graphic_table():
    """The M 5.4DSP has an overlay table; its EQ test frequencies come from it (unchanged)."""
    m = _device("MATCH M 5.4DSP").model
    vec = validate.build_test_vector(m)
    oa = next(o for o in vec.outputs if o.letter == "A")
    assert [b.f for b in oa.eq] == [m.output("A").band_frequency(b.band) for b in oa.eq]
