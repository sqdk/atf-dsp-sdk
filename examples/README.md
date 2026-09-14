# Examples

Runnable demos of the SDK. **They run with no hardware** — by default each uses a
memory-backed *simulated* MATCH M 5.4DSP (`_demo.demo_device()`), so writes round-trip
and reads return them. To run any of them against a real amp, set the port:

```bash
python examples/tune_output.py                    # simulated (offline)
ATF_DSP_SDK_HW=/dev/cu.usbmodemXXXX python examples/tune_output.py   # real amp
```

| Script | Shows |
|---|---|
| [`tune_output.py`](tune_output.py) | Tune one output end-to-end: gain + polarity, crossover, delay, EQ, phase — the fluent chain. |
| [`read_state.py`](read_state.py) | Read the amp back: gain/polarity, recovered crossover, phase, and a full `Setup.from_device()` dump. |
| [`routing.py`](routing.py) | The two routing matrices — set cells and print the grid. |
| [`analyzer_sweep.py`](analyzer_sweep.py) | Measure a response with the Input Signal Analyzer (`InputAnalyzer.sweep`). |
| [`apply_pct6.py`](apply_pct6.py) | Load a PC-Tool `.pct6` tune and preview applying it (dry-run); or dump the device to a `Setup`. |

Everything writable defaults to a dry-run / preview and only the MATCH M 5.4DSP is
hardware-validated — see the top-level README's safety and validation notes.
