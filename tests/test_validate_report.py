"""Offline tests for the submittable-report machinery (validate.report_envelope /
Report.to_dict / format_test_vector). No hardware."""
from __future__ import annotations

import json

import pytest

from atf_dsp import validate
from atf_dsp.device import Device
from atf_dsp.params import ParamMap
from atf_dsp.transport import Link


@pytest.fixture()
def dry_device() -> Device:
    return Device(link=Link(dry_run=True), param_map=ParamMap.load("MatchM54DSP"),
                  model="MATCH M 5.4DSP")


def test_report_to_dict_is_json_serialisable(dry_device):
    rep = validate.exercise_all(dry_device, snapshot=False)
    d = rep.to_dict()
    assert set(d) >= {"direction", "ok", "passed", "failed", "total", "checks", "skipped"}
    assert d["total"] == len(rep.checks)
    json.dumps(d)  # must not raise


def test_report_envelope_has_metadata_and_serialises(dry_device):
    rep = validate.exercise_all(dry_device, snapshot=False)
    env = validate.report_envelope(dry_device, [rep], notes="unit test")
    assert env["schema"] == "atf-dsp-sdk/hw-validation/1"
    assert env["device"]["model"] == "MATCH M 5.4DSP"
    assert env["device"]["fs_hz"] == 48000
    assert env["device"]["fs_confirmed"] is True
    assert env["notes"] == "unit test"
    assert len(env["reports"]) == 1
    json.dumps(env)  # end-to-end serialisable


def test_format_test_vector_lists_every_output(dry_device):
    vec = validate.build_test_vector(dry_device.model)
    txt = validate.format_test_vector(vec)
    for letter in dry_device.model.output_letters:
        assert f"Output {letter}:" in txt
    assert "Test vector" in txt


def test_envelope_carries_brax_sample_rate():
    """A non-48 kHz model's rate flows into the report device block."""
    dev = Device(link=Link(dry_run=True), param_map=ParamMap.load("BraxDSP192kHz"),
                 model="BRAX DSP")
    env = validate.report_envelope(dev, [])
    assert env["device"]["fs_hz"] == 192000
    assert env["device"]["fs_confirmed"] is True
