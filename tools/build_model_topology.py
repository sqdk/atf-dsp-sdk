#!/usr/bin/env python3
"""build_model_topology.py — bake a compact per-model topology table for the SDK.

Scans the vendor ``.at01`` device files, inflates each, runs the model-agnostic
``discover_channels.discover`` on it, and writes a SMALL summary table to
``atf_dsp/data/model_topology.json`` (input/output/routing/EQ counts per model, plus a
``complete`` flag). This lets ``atf_dsp.model_info`` report a model's channel topology
WITHOUT shipping the large (~300 KB each) full param maps — important for the dsp-web
Pyodide bundle, which only needs counts to size its UI and guard untested hardware.

The raw ``.at01`` files are NOT redistributed; only this generated JSON is committed.
Regenerate after adding models or changing discovery:

    python tools/build_model_topology.py [--devicefiles DIR] [--out FILE]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "tools"))

from atf_dsp.models import MODEL_AT01  # noqa: E402
from atf_dsp.params import parse_at01_text  # noqa: E402
from build_param_maps import inflate_at01  # noqa: E402
from discover_channels import discover  # noqa: E402

WORKSPACE = Path("/Users/cp/Documents/code/atf_disassemble")
DEFAULT_DEVICEFILES = WORKSPACE / "extracted" / "app" / "deviceFiles"
DEFAULT_OUT = REPO_ROOT / "atf_dsp" / "data" / "model_topology.json"

# basename -> model name (reverse of MODEL_AT01).
_AT01_MODEL = {v: k for k, v in MODEL_AT01.items()}


def is_complete(topo: dict) -> bool:
    """A topology is 'complete' when discovery resolved every core family — inputs,
    outputs, the output-EQ blocks, and the routing matrix. Incomplete = the model uses
    a param-name spelling discovery doesn't cover yet (guard should treat as untrusted)."""
    rm = topo.get("routing_matrix") or {}
    return bool(topo["inputs"]["count"]
                and topo["outputs"]["count"]
                and topo.get("eq_blocks")
                and rm.get("rows") and rm.get("cols"))


def build(devicefiles: Path) -> dict:
    table: dict = {}
    for at01 in sorted(devicefiles.glob("*.at01")):
        model = _AT01_MODEL.get(at01.stem)
        if model is None:
            continue  # a basename not mapped to a canonical model (e.g. BraxDSP96kHz variant)
        params = parse_at01_text(inflate_at01(at01))
        topo = discover(params)
        topo["complete"] = is_complete(topo)
        table[model] = topo
    return table


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--devicefiles", type=Path, default=DEFAULT_DEVICEFILES)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    a = ap.parse_args()
    table = build(a.devicefiles)
    a.out.write_text(json.dumps(table, indent=1, sort_keys=True))
    complete = sum(1 for t in table.values() if t["complete"])
    print(f"[+] {a.out}: {len(table)} models ({complete} complete, "
          f"{len(table) - complete} partial)")


if __name__ == "__main__":
    main()
