"""contract.py — load the bundled protocol.yaml contract.

Part 2 is an *implementation* of protocol.yaml (see ``docs/encoding.md``): the fixed-point
format and RTA cells are read from the contract rather than hard-coded, so a
contract revision flows through without touching code. The file is bundled at
acodsp/data/protocol.yaml (pinned by its `version:`).
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any, Dict

import yaml

DATA_DIR = Path(__file__).resolve().parent / "data"
CONTRACT_PATH = DATA_DIR / "protocol.yaml"


@lru_cache(maxsize=1)
def load_contract() -> Dict[str, Any]:
    """Return the parsed protocol.yaml contract (cached)."""
    with CONTRACT_PATH.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def contract_version() -> str:
    """Return the pinned contract version string."""
    return str(load_contract().get("version", "unknown"))


def fixed_point() -> Dict[str, Any]:
    """Return the encoders._fixed_point block (format, frac_bits, unity, clamp...)."""
    return load_contract()["encoders"]["_fixed_point"]


def rta_spec() -> Dict[str, Any]:
    """Return the rta block (readback cells, channel-select, peak-reset)."""
    return load_contract()["rta"]
