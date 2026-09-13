"""params.py — per-model parameter map (name -> DSP address) and query helpers.

The map is derived from the vendor `.at01` SigmaStudio export, whose body is a list
of `#define <NAME>_ADDR <n>` lines. Only the generated JSON maps are tracked in the
repo (see acodsp/data/); the raw vendor files are not.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Dict, List, Optional

DATA_DIR = Path(__file__).resolve().parent / "data"

# `#define MOD_FOO_BAR_ADDR   1234` — capture the name (minus the _ADDR suffix) + address.
_DEFINE_RE = re.compile(r"^\s*#define\s+([A-Za-z_][A-Za-z0-9_]*?)_ADDR\s+(\d+)\s*$")


def parse_at01_text(text: str) -> Dict[str, int]:
    """Parse inflated `.at01` text into `{name: addr}` (name has the `_ADDR` stripped)."""
    out: Dict[str, int] = {}
    for line in text.splitlines():
        m = _DEFINE_RE.match(line)
        if m:
            out[m.group(1)] = int(m.group(2))
    return out


def _module_of(name: str) -> str:
    """Best-effort module group for a param name (the token after the MOD_ prefix)."""
    parts = name.split("_")
    if len(parts) >= 2 and parts[0] == "MOD":
        return parts[1]
    return parts[0]


class ParamMap:
    """A parameter map for one model: name -> address, plus lookup/grouping helpers."""

    def __init__(self, params: Dict[str, int], model: Optional[str] = None) -> None:
        self.model = model
        self._params: Dict[str, int] = dict(params)

    # -- construction ------------------------------------------------------
    @classmethod
    def from_at01_text(cls, text: str, model: Optional[str] = None) -> "ParamMap":
        return cls(parse_at01_text(text), model=model)

    @classmethod
    def from_json_file(cls, path: Path) -> "ParamMap":
        data = json.loads(Path(path).read_text())
        return cls(data.get("params", {}), model=data.get("model"))

    @classmethod
    def load(cls, name: str) -> "ParamMap":
        """Load a generated map from acodsp/data/ by basename (e.g. 'MatchM54DSP')."""
        path = DATA_DIR / f"{name}.json"
        if not path.exists():
            raise FileNotFoundError(f"No generated param map at {path}")
        return cls.from_json_file(path)

    # -- queries -----------------------------------------------------------
    def addr(self, name: str) -> int:
        """Return the address for *name* (with or without the trailing `_ADDR`)."""
        key = name[:-5] if name.endswith("_ADDR") else name
        try:
            return self._params[key]
        except KeyError as exc:
            raise KeyError(f"unknown parameter: {name}") from exc

    def get(self, name: str) -> Optional[int]:
        key = name[:-5] if name.endswith("_ADDR") else name
        return self._params.get(key)

    def find(self, substr: str) -> List[str]:
        """Return sorted names containing *substr* (case-insensitive)."""
        s = substr.upper()
        return sorted(n for n in self._params if s in n.upper())

    def names(self) -> List[str]:
        return sorted(self._params)

    def modules(self) -> Dict[str, List[str]]:
        """Group param names by module (the token after MOD_)."""
        groups: Dict[str, List[str]] = {}
        for name in self._params:
            groups.setdefault(_module_of(name), []).append(name)
        for names in groups.values():
            names.sort()
        return groups

    def module(self, name: str) -> List[str]:
        """Return sorted param names belonging to module *name*."""
        return self.modules().get(name, [])

    def to_dict(self) -> Dict[str, int]:
        return dict(self._params)

    def __len__(self) -> int:
        return len(self._params)

    def __contains__(self, name: str) -> bool:
        key = name[:-5] if name.endswith("_ADDR") else name
        return key in self._params

    def __repr__(self) -> str:
        return f"ParamMap(model={self.model!r}, params={len(self._params)})"
