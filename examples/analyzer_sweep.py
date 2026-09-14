"""Measure the amp's response with the on-chip Input Signal Analyzer.

The analyzer is a HOST-driven swept bandpass: for each frequency the SDK retunes one
analysis biquad, waits, and reads the level readback cell. Here we run a short sweep.

Offline (simulated device) the readback cell is constant, so the curve is flat — the point
is to show the API and the Spectrum result. On a real amp (ATF_DSP_SDK_HW=<port>) with a
tone playing, the peak tracks the tone.

Run:  python examples/analyzer_sweep.py
"""
from __future__ import annotations

from _demo import demo_device

from atf_dsp import InputAnalyzer


def main() -> None:
    with demo_device() as dev:
        analyzer = InputAnalyzer(dev)
        # a few log-spaced points; no-op sleep so the demo is instant offline
        spectrum = analyzer.sweep([50, 100, 250, 1000, 4000, 10000], sleep=lambda *_: None)

        f_peak, level = spectrum.peak()
        print(f"swept {len(spectrum.freqs)} points from {spectrum.freqs[0]:.0f} Hz "
              f"to {spectrum.freqs[-1]:.0f} Hz")
        print(f"peak: {level:.1f} dBFS @ {f_peak:.0f} Hz")


if __name__ == "__main__":
    main()
