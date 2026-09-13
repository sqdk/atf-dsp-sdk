"""Offline param-map tests: parsing, lookup, find, module grouping, JSON load."""
from __future__ import annotations

import pytest

from acodsp.params import ParamMap, parse_at01_text

SAMPLE = """\
#define HEADERVERSION N6
#define MATCHINGAF MatchM54DSP_1.AF1
#define PGM 4568
#define MOD_INPUTGAIN_ALG0_MUTE0_ADDR                  4577
#define MOD_INPUTGAIN_ALG0_MUTE1_ADDR                  4578
#define MOD_ADCCOMP_ALG0_STAGE0_B2_ADDR                4589
#define MOD_ADCCOMP_ALG0_STAGE0_A1_ADDR                4593
#define MOD_VOLUME_READBACK_VOLRB_READBACK_X_ADDR 4600
"""


def test_parse_extracts_only_addr_defines():
    params = parse_at01_text(SAMPLE)
    assert params["MOD_INPUTGAIN_ALG0_MUTE0"] == 4577
    assert params["MOD_ADCCOMP_ALG0_STAGE0_B2"] == 4589
    # Metadata defines without an _ADDR suffix are ignored.
    assert "HEADERVERSION" not in params
    assert "PGM" not in params
    assert len(params) == 5


def test_addr_lookup_with_and_without_suffix():
    pm = ParamMap.from_at01_text(SAMPLE, model="TEST")
    assert pm.addr("MOD_INPUTGAIN_ALG0_MUTE1") == 4578
    assert pm.addr("MOD_INPUTGAIN_ALG0_MUTE1_ADDR") == 4578


def test_addr_unknown_raises():
    pm = ParamMap.from_at01_text(SAMPLE)
    with pytest.raises(KeyError):
        pm.addr("NOPE")


def test_find_is_case_insensitive_substring():
    pm = ParamMap.from_at01_text(SAMPLE)
    hits = pm.find("mute")
    assert hits == ["MOD_INPUTGAIN_ALG0_MUTE0", "MOD_INPUTGAIN_ALG0_MUTE1"]


def test_module_grouping():
    pm = ParamMap.from_at01_text(SAMPLE)
    mods = pm.modules()
    assert "INPUTGAIN" in mods
    assert "ADCCOMP" in mods
    assert pm.module("INPUTGAIN") == [
        "MOD_INPUTGAIN_ALG0_MUTE0",
        "MOD_INPUTGAIN_ALG0_MUTE1",
    ]


def test_contains_and_len():
    pm = ParamMap.from_at01_text(SAMPLE)
    assert "MOD_ADCCOMP_ALG0_STAGE0_A1" in pm
    assert "MOD_ADCCOMP_ALG0_STAGE0_A1_ADDR" in pm
    assert len(pm) == 5


def test_generated_matchm54dsp_map_loads():
    pm = ParamMap.load("MatchM54DSP")
    assert pm.model == "MATCH M 5.4DSP"
    assert len(pm) > 3000
    # A few known-good addresses from the inflated .at01.
    assert pm.addr("MOD_SAFELOADMODULE_DATA_SAFELOAD0") == 4568
    assert pm.addr("MOD_INPUTGAIN_ALG0_MUTE0") == 4577
    assert pm.find("SAFELOAD")  # non-empty
