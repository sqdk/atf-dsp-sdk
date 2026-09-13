#!/usr/bin/env python3
"""discover_channels.py — derive a model's channel topology from its inflated .at01 map.

Model-agnostic: input/output counts, EQ blocks + band counts, gain/delay/mute/xover
families, and the routing-matrix dimensions are all DISCOVERED from the param names,
never hardcoded (input count depends on the device; output meaning depends on routing).

Emits a structural channel-map JSON. The *semantic* overlay (which EQ block is
virtual vs output, human labels, virtual-channel identities) is intentionally NOT
decided here — it is a small provisional overlay confirmed against the PC-Tool UI.

Usage: discover_channels.py <inflated_at01.h> [--json out.json]
"""
import sys, re, json, argparse
from collections import defaultdict

RX = re.compile(r"#define\s+(\S+?)_ADDR\s+(\d+)")


def load(path):
    m = {}
    for line in open(path, errors="ignore"):
        g = RX.search(line)
        if g:
            m[g.group(1)] = int(g.group(2))
    return m


def stage_indices(names):
    st = set()
    for n in names:
        g = re.search(r"STAGE(\d+)", n)
        if g:
            st.add(int(g.group(1)))
    return sorted(st)


def discover(params):
    names = list(params)
    out = {"param_count": len(names)}

    # --- physical inputs: INPUT_EQ_LINE_INPUTEQ{X} letters + INPUTGAIN mutes ---
    in_letters = sorted({m.group(1) for n in names
                         for m in [re.search(r"INPUT_EQ_LINE_INPUTEQ([A-Z])", n)] if m})
    in_mutes = sorted({int(m.group(1)) for n in names
                      for m in [re.search(r"INPUTGAIN_ALG0_MUTE(\d+)", n)] if m})
    out["inputs"] = {
        "count": len(in_letters), "letters": in_letters, "mute_indices": in_mutes,
        "eq_bands": len(stage_indices(n for n in names if "INPUTEQ" in n and "STAGE" in n)),
    }

    # --- physical outputs: GAIN{X}, FILTERS xover {X}, DELAY{X}, OUTPUTMUTE{n} ---
    out_gain = sorted({m.group(1) for n in names
                      for m in [re.search(r"MOD_GAIN_GAIN([A-Z])_", n)] if m})
    out_xover = sorted({m.group(1) for n in names
                       for m in [re.search(r"FILTERS__PHASE_12DB_([A-Z])", n)] if m})
    out_delay = sorted({m.group(1) for n in names
                       for m in [re.search(r"DELAY__PHASE_SWITCH_DELAY([A-Z])_", n)] if m})
    out_mutes = sorted({int(m.group(1)) for n in names
                       for m in [re.search(r"OUTPUTMUTE_ALG0_MUTE(\d+)", n)] if m})
    out["outputs"] = {
        "count": len(out_gain), "letters": out_gain, "xover_letters": out_xover,
        "delay_letters": out_delay, "mute_indices": out_mutes,
    }

    # --- EQ blocks: the output EQUALIZER cascade, reported as EQ1/EQ2 sub-blocks ---
    # Two vendor naming axes seen across models (same underlying structure — a 2-block
    # cascade of 15 + 16 bands per output):
    #   * block-first  (M 5.4DSP):  MOD_EQUALIZER_EQ1_9_ALG0 / EQ2_9   (block=1/2, inst=output number)
    #   * letter-first (UP 10DSP MK2): MOD_EQUALIZER_EQ_A1_ALG0 / EQ_K2 (inst=output letter, block=1/2)
    # An unnumbered EQ1/EQ2 (single instance) is also tolerated. Normalise both to EQ{block}.
    _blk_insts: "defaultdict[str, set]" = defaultdict(set)
    _blk_bands: "defaultdict[str, set]" = defaultdict(set)
    for n in names:
        m = re.search(r"MOD_EQUALIZER_EQ([12])(?:_(\d+))?_ALG0", n)   # block-first
        if m:
            blk, inst = f"EQ{m.group(1)}", (m.group(2) or "1")
        else:
            m = re.search(r"MOD_EQUALIZER_EQ_([A-Za-z]+?)([12])_ALG0", n)   # letter-first
            if not m:
                continue
            blk, inst = f"EQ{m.group(2)}", m.group(1)
        _blk_insts[blk].add(inst)
        b = re.search(r"STAGE(\d+)", n)
        if b:
            _blk_bands[blk].add(int(b.group(1)))
    out["eq_blocks"] = {blk: {"instances": len(_blk_insts[blk]), "bands": len(_blk_bands[blk])}
                        for blk in sorted(_blk_insts)}

    # --- role / virtual EQ + gain (GLOBAL_EQ zones, GAIN role prefixes) ---
    roles = sorted({m.group(1) for n in names
                   for m in [re.search(r"MOD_GLOBAL_EQ_([A-Z_]+?)_GLOBALEQ", n)] if m})
    gain_roles = sorted({m.group(1) for n in names
                        for m in [re.search(r"MOD_GAIN_([A-Z]+)_GAIN[A-Z]+_", n)] if m})
    out["roles"] = {"global_eq_zones": roles, "gain_role_prefixes": gain_roles}

    # --- routing matrix: VOL{row}{col} -> dimensions ---
    cells = [(m.group(1), m.group(2)) for n in names
             for m in [re.search(r"NXNMIX\w*VOL(\d{2})(\d{2})", n)] if m]
    if cells:
        rows = sorted({r for r, _ in cells}); cols = sorted({c for _, c in cells})
        out["routing_matrix"] = {"rows": len(rows), "cols": len(cols),
                                 "cells": len(cells), "orientation": "rows x cols (provisional: rows=outputs, cols=virtual)"}
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("at01")
    ap.add_argument("--json")
    a = ap.parse_args()
    d = discover(load(a.at01))
    js = json.dumps(d, indent=2)
    if a.json:
        open(a.json, "w").write(js)
        print(f"[+] wrote {a.json}")
    print(js)


if __name__ == "__main__":
    main()
