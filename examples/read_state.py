"""Read the amp's current state back: per-output gain/polarity, the recovered crossover,
the phase all-pass, and a full setup dump.

Offline, we write a small tune first so the simulated device has something to read. On a
real amp (ATF_DSP_SDK_HW=<port>), delete the "seed a tune" block to read the LIVE tune.

Run:  python examples/read_state.py
"""
from __future__ import annotations

from _demo import demo_device

from atf_dsp import Setup


def main() -> None:
    with demo_device() as dev:
        # --- seed a tune so the offline device has state (skip on real hardware) ---
        (dev.model.output("A")
            .gain(-6.0, inverted=True)
            .crossover(hp=100, characteristic="butterworth", slope=24)
            .phase(45, ref_hz=100))

        # --- read it back ---
        out = dev.model.output("A")
        print(f"Output A: {out.gain_db():+.1f} dB, inverted={out.is_inverted()}")
        hp = out.recover_crossover()["highpass"]
        print(f"  crossover HP: {hp.corner_hz:.0f} Hz · {hp.slope_db} dB/oct · {hp.characteristic}")
        print(f"  phase: {out.phase_deg():.0f}°")

        # --- dump the whole device into a portable Setup object ---
        setup = Setup.from_device(dev)
        print(f"read {len(setup.outputs)} outputs / {len(setup.virtuals)} virtual / "
              f"{len(setup.inputs)} inputs into a Setup")


if __name__ == "__main__":
    main()
