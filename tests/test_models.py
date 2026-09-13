"""Tests for the model registry + support-metadata guard API (acodsp.models)."""
import acodsp
from acodsp import models


def test_model_info_hardware_confirmed_m54():
    """The M 5.4DSP is the one fully-supported, hardware-confirmed model."""
    info = acodsp.model_info(0x2008)  # by PID
    assert info["model"] == "MATCH M 5.4DSP"
    assert info["pid"] == 0x2008
    assert info["dev_id"] == 29
    assert info["fs_hz"] == 48000 and info["fs_confirmed"] is True
    assert info["has_param_map"] and info["has_channel_model"]
    assert info["hardware_confirmed"] and info["supported"]
    assert info["verification"] == "hardware-confirmed"
    assert info["warnings"] == []
    # Lookup by name is equivalent.
    assert acodsp.model_info("MATCH M 5.4DSP") == info


def test_model_info_discoverable_but_unsupported():
    """A model with an .at01 but no shipped channel model: onboardable, not driveable."""
    info = acodsp.model_info("MATCH UP 10DSP MK2")
    assert info["model"] == "MATCH UP 10DSP MK2"
    assert info["verification"] == "discoverable"
    assert info["supported"] is False and info["hardware_confirmed"] is False
    assert info["has_channel_model"] is False
    assert any("No channel model" in w for w in info["warnings"])
    assert any("NOT confirmed" in w for w in info["warnings"])


def test_model_info_multirate_fs_from_at01_basename():
    """BRAX ships multi-rate firmware; fs is derived from the .at01 basename and flags 48k drift."""
    info = acodsp.model_info("BRAX DSP")
    assert info["fs_hz"] == 192000 and info["fs_confirmed"] is True
    assert any("48 kHz" in w and "192000" in w for w in info["warnings"])


def test_model_info_unknown_pid():
    info = acodsp.model_info(0x9999)
    assert info["model"] is None
    assert info["verification"] == "unknown" and info["supported"] is False
    assert any("Unknown USB PID" in w for w in info["warnings"])


def test_data_availability_helpers():
    assert models.has_param_map("MATCH M 5.4DSP") is True
    assert models.has_channel_model("MATCH M 5.4DSP") is True
    assert models.has_channel_model("MATCH UP 10DSP MK2") is False
    assert models.has_param_map(None) is False


def test_topology_baked_for_all_match_models():
    """Every MATCH model has a complete baked topology (the user's hardware family)."""
    table = models._topology_table()
    match_models = [m for m in table if m.startswith("MATCH")]
    assert len(match_models) >= 10
    for m in match_models:
        assert table[m]["complete"] is True, f"{m} topology incomplete"


def test_model_info_includes_topology_counts():
    m54 = acodsp.model_info("MATCH M 5.4DSP")
    assert m54["input_count"] == 4 and m54["output_count"] == 9
    assert m54["topology"]["complete"] is True

    up10 = acodsp.model_info("MATCH UP 10DSP MK2")
    assert up10["input_count"] == 8 and up10["output_count"] == 11
    assert up10["topology"]["eq_blocks"]["EQ2"] == {"instances": 11, "bands": 16}


def test_model_info_flags_incomplete_topology():
    """High-end models whose spelling discovery doesn't fully cover are flagged, not hidden."""
    info = acodsp.model_info("HELIX DSP ULTRA")
    assert info["topology"] is not None and info["topology"]["complete"] is False
    assert any("INCOMPLETE" in w for w in info["warnings"])
