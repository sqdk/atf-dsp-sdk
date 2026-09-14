"""Drive the routing matrices — the two NxN mixers in the MATCH signal chain
(inputs → virtual tuning channels → outputs).

Run:  python examples/routing.py
"""
from __future__ import annotations

from _demo import demo_device


def main() -> None:
    with demo_device() as dev:
        routing = dev.model.routing

        # virtual → output matrix: send Virtual A to Output A at 0 dB (unity)
        v2o = routing.virtual_to_output
        v2o.set("A", "A", 0.0)                 # row = output, col = virtual
        v2o.set("B", "A", -3.0)                # also feed Output B from Virtual A, -3 dB

        # input → virtual matrix: route Input A into Virtual A
        routing.input_to_virtual.set("A", "A", 0.0)

        # read the matrix back off the device, then print the grid. Cells are LINEAR mix
        # gains (1.0 = unity = 0 dB, 0.71 ≈ -3 dB); set() above took dB. None = no path.
        v2o.read(dev)
        print(f"virtual→output ({v2o.rows}×{v2o.cols}), linear (1.0 = unity):")
        for r, row in enumerate(v2o.as_grid()):
            cells = " ".join("  ·  " if v is None else f"{v:4.2f}" for v in row)
            print(f"  out {chr(ord('A') + r)}: {cells}")


if __name__ == "__main__":
    main()
