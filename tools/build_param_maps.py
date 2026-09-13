#!/usr/bin/env python3
"""build_param_maps.py — generate acodsp/data/<model>.json from vendor .at01 files.

Reads the SigmaStudio param exports from the research workspace and emits one JSON
map per model. Two input shapes are supported:

  * an already-inflated `.h` (plain `#define <NAME>_ADDR n` text), e.g.
    analysis/MatchM54DSP.at01.inflated.h
  * a raw `.at01` (a 4-byte length header followed by a zlib stream), e.g.
    extracted/app/deviceFiles/*.at01

The raw vendor files are NOT committed; only the generated JSON maps are.

Usage:
    python tools/build_param_maps.py <input.at01|input.h> [more...] [--out DIR]
    python tools/build_param_maps.py --scan <deviceFiles_dir> [--out DIR]
    python tools/build_param_maps.py            # defaults: the workspace MatchM54DSP inputs
"""
from __future__ import annotations

import argparse
import json
import sys
import zlib
from pathlib import Path
from typing import Dict, Optional

# Allow running from a source checkout without installing.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from acodsp.models import MODEL_AT01  # noqa: E402
from acodsp.params import parse_at01_text  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = REPO_ROOT / "acodsp" / "data"

# Research workspace (source of truth; not redistributed).
WORKSPACE = Path("/Users/cp/Documents/code/atf_disassemble")
DEFAULT_INFLATED = WORKSPACE / "analysis" / "MatchM54DSP.at01.inflated.h"
DEFAULT_DEVICEFILES = WORKSPACE / "extracted" / "app" / "deviceFiles"

# Reverse of MODEL_AT01: basename -> model name.
_AT01_MODEL = {v: k for k, v in MODEL_AT01.items()}


def inflate_at01(path: Path) -> str:
    """Return the text of an .at01: raw zlib (after a 4-byte header) or plain text."""
    raw = path.read_bytes()
    # Plain inflated text already?
    if raw.lstrip()[:7] == b"#define":
        return raw.decode("utf-8", "replace")
    # Raw .at01: 4-byte big-endian uncompressed-length header, then a zlib stream.
    for skip in (4, 0):
        try:
            return zlib.decompress(raw[skip:]).decode("utf-8", "replace")
        except zlib.error:
            continue
    raise ValueError(f"could not inflate {path}: not plain text or zlib(after 4-byte header)")


def basename_for(path: Path) -> str:
    """Derive the canonical .at01 basename from an input path."""
    name = path.name
    for suffix in (".at01.inflated.h", ".inflated.h", ".at01.h", ".at01", ".h"):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return path.stem


def build_one(path: Path, out_dir: Path) -> Optional[Path]:
    """Build one JSON map from an input file; return the written path (or None)."""
    text = inflate_at01(path)
    params: Dict[str, int] = parse_at01_text(text)
    if not params:
        print(f"[!] {path.name}: no `#define ..._ADDR` lines found — skipping", file=sys.stderr)
        return None
    basename = basename_for(path)
    model = _AT01_MODEL.get(basename)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{basename}.json"
    payload = {
        "model": model,
        "at01": basename,
        "source": path.name,
        "param_count": len(params),
        "params": params,
    }
    out_path.write_text(json.dumps(payload, indent=1, sort_keys=True) + "\n")
    print(f"[+] {out_path.name}: {len(params)} params (model={model})")
    return out_path


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("inputs", nargs="*", help="input .at01 / inflated .h files")
    ap.add_argument("--scan", metavar="DIR", help="build every *.at01 in DIR")
    ap.add_argument("--out", default=str(DEFAULT_OUT), help="output dir (default acodsp/data)")
    args = ap.parse_args(argv)

    out_dir = Path(args.out)
    inputs = [Path(p) for p in args.inputs]
    if args.scan:
        inputs += sorted(Path(args.scan).glob("*.at01"))
    if not inputs:
        # Default: the workspace MATCH M 5.4DSP inputs.
        if DEFAULT_INFLATED.exists():
            inputs = [DEFAULT_INFLATED]
        elif (DEFAULT_DEVICEFILES / "MatchM54DSP.at01").exists():
            inputs = [DEFAULT_DEVICEFILES / "MatchM54DSP.at01"]
        else:
            ap.error("no inputs given and no default workspace files found")

    written = 0
    for path in inputs:
        if not path.exists():
            print(f"[!] missing: {path}", file=sys.stderr)
            continue
        try:
            if build_one(path, out_dir):
                written += 1
        except (ValueError, OSError) as exc:
            print(f"[!] {path.name}: {exc}", file=sys.stderr)
    print(f"[=] wrote {written} map(s) to {out_dir}")
    return 0 if written else 1


if __name__ == "__main__":
    raise SystemExit(main())
