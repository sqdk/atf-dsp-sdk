"""Work with PC-Tool .pct6 setup files.

Two things this shows:
  1. Load a vendor .pct6 tune and PREVIEW applying it to the amp (dry-run — nothing written):
         python examples/apply_pct6.py path/to/tune.pct6
  2. With no file, read the (simulated) device into a Setup and preview re-applying it —
     runnable offline, no .pct6 needed:
         python examples/apply_pct6.py
"""
from __future__ import annotations

import sys

from _demo import demo_device

from atf_dsp import Setup


def main(argv: list) -> None:
    with demo_device() as dev:
        if len(argv) > 1:
            setup = Setup.load(argv[1])
            print(f"loaded {argv[1]}: {len(setup.outputs)} outputs, {len(setup.virtuals)} virtual")
        else:
            setup = Setup.from_device(dev)
            print(f"read device into a Setup: {len(setup.outputs)} outputs, {len(setup.virtuals)} virtual")

        # dry_run=True builds the write plan but never transmits to a real amp.
        result = dev.apply_setup(setup, dry_run=True)
        print(f"apply preview (dry-run): {result.writes} writes planned, {result.skipped} skipped, "
              f"{result.frames} frames built")


if __name__ == "__main__":
    main(sys.argv)
