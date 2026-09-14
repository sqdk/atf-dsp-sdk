"""Tune one output end-to-end in the fluent channel model:
gain + polarity, crossover, delay, EQ, and phase — a complete driver setup in a few lines.

Run offline (simulated amp) with:  python examples/tune_output.py
"""
from __future__ import annotations

from _demo import demo_device


def main() -> None:
    with demo_device() as dev:
        (dev.model.output("A")
            .gain(-3.0, inverted=True)                                   # -3 dB, polarity inverted
            .crossover(hp=80, lp=2500, characteristic="linkwitz_riley", slope=24)
            .delay_ms(1.5)                                               # time-alignment (fs-correct)
            .eq_band(0, f=120, Q=0.7, gain_db=+2.0)                      # a couple of parametric bands
            .eq_band(5, f=3000, Q=1.5, gain_db=-3.0)
            .phase(90, ref_hz=80))                                       # 90° all-pass at the xover

        # Read part of it back to confirm it landed:
        out = dev.model.output("A")
        print(f"Output A on {dev.model_name} @ {dev.fs} Hz: "
              f"{out.gain_db():+.1f} dB, inverted={out.is_inverted()}")


if __name__ == "__main__":
    main()
