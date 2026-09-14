"""models.py — USB VID/PID -> model table and per-model device-file mapping.

VID is Audiotec Fischer (0x2E4F); the PID identifies the model. The firmware is
shared across all 35 ACO models, so only the PID and the per-model .at01 param map
differ. `at01_for_model` names the SigmaStudio export basename (which is also the
generated JSON basename in atf_dsp/data/).
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Dict, List, Optional

VID = 0x2E4F

_DATA_DIR = Path(__file__).resolve().parent / "data"

# Precomputed per-model channel topology (input/output/routing/EQ counts + a `complete`
# flag), baked by tools/build_model_topology.py so model_info() can report shape without
# shipping the large full param maps. Loaded lazily + cached.
_TOPOLOGY_CACHE: Optional[Dict[str, dict]] = None


def _topology_table() -> Dict[str, dict]:
    global _TOPOLOGY_CACHE
    if _TOPOLOGY_CACHE is None:
        path = _DATA_DIR / "model_topology.json"
        try:
            _TOPOLOGY_CACHE = json.loads(path.read_text())
        except (OSError, ValueError):
            _TOPOLOGY_CACHE = {}
    return _TOPOLOGY_CACHE


def topology_for_model(model: Optional[str]) -> Optional[dict]:
    """Return the baked channel topology for a model (counts/shapes), or None if absent."""
    if model is None:
        return None
    return _topology_table().get(model)

# PID -> model, transcribed from atf_aco_usb_driver.inf (see atf_dsp.py PID_MODELS).
PID_MODELS = {
    0x3000: "BRAX DSP",
    0x3001: "HELIX DSP MINI",
    0x3005: "HELIX DSP MINI MK2",
    0x3002: "HELIX DSP.3",
    0x3006: "HELIX DSP.3S",
    0x3004: "HELIX DSP PRO MK3",
    0x3003: "HELIX DSP ULTRA",
    0x3007: "HELIX DSP ULTRA S",
    0x3008: "HELIX DSP ULTRA XT",
    0x2015: "HELIX AMPLIFY 206 DSP",
    0x2017: "HELIX ISM 400.2DSP",
    0x2004: "HELIX M FOUR DSP",
    0x2009: "HELIX M FOUR DSP",
    0x200C: "HELIX M SIX DSP",
    0x2005: "HELIX M SIX DSP",
    0x2001: "HELIX V EIGHT DSP MK2",
    0x2012: "HELIX V EIGHT DSP ULTIMATE",
    0x2000: "HELIX V TWELVE DSP",
    0x200A: "HELIX V TWELVE DSP MK2",
    0x200D: "HELIX V EIGHTEEN DSP",
    0x2002: "HELIX P SIX DSP ULTIMATE",
    0x2003: "MATCH M 5DSP MK2",
    0x2008: "MATCH M 5.4DSP",
    0x200B: "MATCH PP 86DSP MK2",
    0x2016: "MATCH UP 4DSP",
    0x200F: "MATCH UP 6DSP",
    0x201D: "MATCH UP 6DSP MK2",
    0x2007: "MATCH UP 8DSP",
    0x201A: "MATCH UP 8DSP MK2",
    0x2010: "MATCH UP 8BMW",
    0x2020: "MATCH UP 8BMW MK2",
    0x2006: "MATCH UP 10DSP",
    0x2011: "MATCH UP 10DSP - 24V",
    0x201E: "MATCH UP 10DSP MK2",
}

# model name -> device-file (.at01) basename. This basename is also the generated
# JSON basename under atf_dsp/data/. BRAX ships two rates; default to 192 kHz.
MODEL_AT01 = {
    "BRAX DSP": "BraxDSP192kHz",
    "HELIX DSP MINI": "HelixDSPMini",
    "HELIX DSP MINI MK2": "HelixDSPMiniMK2",
    "HELIX DSP.3": "HelixDSP3",
    "HELIX DSP.3S": "HelixDSP3S",
    "HELIX DSP PRO MK3": "HelixDSPProMK3",
    "HELIX DSP ULTRA": "HelixDSPUltra",
    "HELIX DSP ULTRA S": "HelixDSPUltraS",
    "HELIX DSP ULTRA XT": "HelixDSPUltraXT",
    "HELIX AMPLIFY 206 DSP": "HelixAMPLIFY206DSP",
    "HELIX ISM 400.2DSP": "HelixISM400DSP",
    "HELIX M FOUR DSP": "HelixMFourDSP",
    "HELIX M SIX DSP": "HelixMSixDSP",
    "HELIX V EIGHT DSP MK2": "HelixVEightDSPMK2",
    "HELIX V EIGHT DSP ULTIMATE": "HelixVEightDSPUltimate",
    "HELIX V TWELVE DSP": "HelixVTwelveDSP",
    "HELIX V TWELVE DSP MK2": "HelixVTwelveDSPMK2",
    "HELIX V EIGHTEEN DSP": "HelixVEighteenDSP",
    "HELIX P SIX DSP ULTIMATE": "HelixPSixDSPUltimate",
    "MATCH M 5DSP MK2": "MatchM5DSPMK2",
    "MATCH M 5.4DSP": "MatchM54DSP",
    "MATCH PP 86DSP MK2": "MatchPP86DSPMK2",
    "MATCH UP 4DSP": "MatchUP4DSP",
    "MATCH UP 6DSP": "MatchUP6DSP",
    "MATCH UP 6DSP MK2": "MatchUP6DSPMK2",
    "MATCH UP 8DSP": "MatchUP8DSP",
    "MATCH UP 8DSP MK2": "MatchUP8DSPMK2",
    "MATCH UP 8BMW": "MatchUP8BMW",
    "MATCH UP 8BMW MK2": "MatchUP8BMWMK2",
    "MATCH UP 10DSP": "MatchUP10DSP",
    "MATCH UP 10DSP - 24V": "MatchUP10DSP24V",
    "MATCH UP 10DSP MK2": "MatchUP10DSPMK2",
}


# model name -> PC-Tool INTERNAL device-type id (the ``<ATF Dev>`` attribute in a .pct6).
# This is the PC-Tool's OWN model enumeration and is DISTINCT from the USB PID: the USB
# VID/PID (see PID_MODELS) identify the plugged-in USB device, whereas ``Dev`` identifies
# the model recorded inside a saved setup file. The two live in different number spaces —
# never conflate them (e.g. the MATCH M 5.4DSP is USB PID 0x2008 but ``Dev``=29).
#
# Only values VERIFIED against a real vendor .pct6 are listed; every other model returns
# None (callers degrade gracefully) rather than guessing an id.
#   VERIFIED: MATCH M 5.4DSP -> Dev=29 (user's real ~/Downloads/new-10-new-eq-2.pct6).
DEV_IDS = {
    "MATCH M 5.4DSP": 29,
}


# --- support / verification metadata (for host apps to guard untested hardware) -------
#
# The SDK ships a full driveable model (param map + <model>.channels.yaml) ONLY for the
# MATCH M 5.4DSP, and that is the only model whose semantics are HARDWARE-CONFIRMED (bench
# 2026-08-29→31). Every other model can be *onboarded* offline from its .at01 (topology
# discovers correctly) but has no shipped channel model yet, and NONE are hardware-confirmed.
# A consuming app (e.g. the dsp-web bridge) should call :func:`model_info` and refuse or
# warn on anything that isn't "hardware-confirmed".

# Models whose control semantics have been validated on real silicon.
HARDWARE_CONFIRMED = frozenset({"MATCH M 5.4DSP"})

# Nominal DSP sample rate (Hz) where known/confirmed. The whole ACO/MATCH line runs at
# 48 kHz; BRAX ships multi-rate firmware (the .at01 basename carries the rate). Absent an
# entry here, fs is derived from the .at01 basename (…96kHz/…192kHz) else assumed 48 kHz
# UNCONFIRMED. The SDK's fluent encoders assume 48 kHz — see model_info()['warnings'].
MODEL_FS: Dict[str, int] = {
    "MATCH M 5.4DSP": 48000,   # bench-confirmed
}


def _fs_for(model: Optional[str]) -> tuple[Optional[int], bool]:
    """(sample_rate_hz, confirmed). Explicit map wins; else parse a kHz suffix from the
    .at01 basename (e.g. BraxDSP192kHz); else assume 48000 UNCONFIRMED."""
    if model in MODEL_FS:
        return MODEL_FS[model], True
    basename = MODEL_AT01.get(model or "", "")
    m = re.search(r"(\d+)kHz", basename)
    if m:
        return int(m.group(1)) * 1000, True
    return (48000, False) if model in MODEL_AT01 else (None, False)


def has_param_map(model: Optional[str]) -> bool:
    """True if a generated ``<basename>.json`` param map ships for this model."""
    b = MODEL_AT01.get(model or "")
    return bool(b) and (_DATA_DIR / f"{b}.json").is_file()


def has_channel_model(model: Optional[str]) -> bool:
    """True if a ``<basename>.channels.yaml`` semantic overlay ships (fluent API works)."""
    b = MODEL_AT01.get(model or "")
    return bool(b) and (_DATA_DIR / f"{b}.channels.yaml").is_file()


def model_info(model_or_pid) -> Dict:
    """Return a metadata + support descriptor for a model, keyed by model name or USB PID.

    Intended for host apps (e.g. the dsp-web bridge) to identify the connected device and
    GUARD against untested hardware before allowing any writes. Fields:

    * ``model`` / ``pid`` / ``dev_id`` — identity (canonical name; USB PID; PC-Tool ``<ATF Dev>`` id)
    * ``fs_hz`` / ``fs_confirmed`` — nominal sample rate and whether it is trustworthy
    * ``has_param_map`` / ``has_channel_model`` — what SDK data ships (drivability)
    * ``hardware_confirmed`` — semantics validated on real silicon
    * ``supported`` — safe to drive now (has a channel model AND is hardware-confirmed)
    * ``verification`` — ``hardware-confirmed`` | ``data-available`` | ``discoverable`` | ``unknown``
    * ``topology`` — baked channel shape (inputs/outputs/routing/eq_blocks + ``complete`` flag), or None
    * ``input_count`` / ``output_count`` — convenience shortcuts pulled from ``topology``
    * ``warnings`` — human-readable guard messages (empty when fully supported)
    """
    pid = model_or_pid if isinstance(model_or_pid, int) else None
    name = model_for_pid(pid) if pid is not None else model_or_pid

    chan = has_channel_model(name)
    hw = name in HARDWARE_CONFIRMED
    fs_hz, fs_conf = _fs_for(name)
    known = name in MODEL_AT01
    topo = topology_for_model(name)

    if name is None or not known:
        verification = "unknown"
    elif hw:
        verification = "hardware-confirmed"
    elif chan:
        verification = "data-available"
    else:
        verification = "discoverable"
    supported = chan and hw

    warnings: List[str] = []
    if name is None:
        warnings.append(
            f"Unknown USB PID {pid:#06x} — not an Audiotec Fischer ACO model in the registry."
            if pid is not None else "Unknown model — not in the registry.")
    elif not known:
        warnings.append(f"'{name}' is not a known ACO model.")
    else:
        if not chan:
            warnings.append(
                f"No channel model ships for {name}; only hardware-confirmed models are driveable. "
                "Channel controls (gain/EQ/crossover/routing) are unavailable.")
        elif not hw:
            warnings.append(
                f"{name} is not hardware-confirmed — its channel model is unverified on real "
                "silicon. Treat any write as EXPERIMENTAL.")
        if fs_hz and fs_hz != 48000:
            warnings.append(
                f"Sample rate {fs_hz} Hz: the SDK's fluent encoders assume 48 kHz, so EQ/crossover/"
                "delay/analyzer frequencies will be off by fs/48000 unless fs is passed explicitly.")
        if not fs_conf:
            warnings.append(f"Sample rate for {name} is assumed 48 kHz but NOT confirmed.")
        if topo is None:
            warnings.append(f"No baked channel topology for {name} (unknown shape).")
        elif not topo.get("complete"):
            warnings.append(
                f"Channel topology for {name} is INCOMPLETE — discovery could not resolve every "
                "family (this model uses a param-name spelling not yet covered). Counts may be partial.")

    return {
        "model": name if known else None,
        "pid": pid if pid is not None else next((p for p, n in PID_MODELS.items() if n == name), None),
        "dev_id": dev_id_for_model(name),
        "at01": MODEL_AT01.get(name or ""),
        "fs_hz": fs_hz,
        "fs_confirmed": fs_conf,
        "has_param_map": has_param_map(name),
        "has_channel_model": chan,
        "hardware_confirmed": hw,
        "supported": supported,
        "verification": verification,
        "topology": topo,
        "input_count": (topo or {}).get("inputs", {}).get("count") if topo else None,
        "output_count": (topo or {}).get("outputs", {}).get("count") if topo else None,
        "warnings": warnings,
    }


def model_for_pid(pid: Optional[int]) -> Optional[str]:
    """Return the model name for a USB PID, or None if unknown."""
    if pid is None:
        return None
    return PID_MODELS.get(pid)


def dev_id_for_model(model: Optional[str]) -> Optional[int]:
    """Return the PC-Tool INTERNAL device-type id (the ``<ATF Dev>`` attribute) for a
    model name, or None if unknown. This is NOT the USB PID — see :data:`DEV_IDS`."""
    if model is None:
        return None
    return DEV_IDS.get(model)


def at01_for_model(model: Optional[str]) -> Optional[str]:
    """Return the .at01 / JSON basename for a model name, or None if unknown."""
    if model is None:
        return None
    return MODEL_AT01.get(model)


def at01_for_pid(pid: Optional[int]) -> Optional[str]:
    """Convenience: PID -> device-file basename."""
    return at01_for_model(model_for_pid(pid))
