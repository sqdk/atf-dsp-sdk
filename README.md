# atf-dsp-sdk

Python SDK for programmatic control of **Audiotec Fischer** ACO-platform DSP
amplifiers (MATCH / HELIX / BRAX) over USB, built on a reverse-engineered
serial protocol.

The vendor ships a Windows-only tuning GUI (DSP PC-Tool). `atf-dsp-sdk` exposes
the same amp as a scriptable Python object — read/write DSP parameters, switch
setups, drive EQ / delay / gain per channel, and read the on-chip Input Signal
Analyzer — from macOS, Linux, or Windows.

> **Independent, unofficial, and built exclusively for programmatic
> interoperability.** This project is **not affiliated with, endorsed by,
> sponsored by, or supported by Audiotec Fischer GmbH** in any way. It exists
> solely to let software interoperate with a device the author lawfully owns.
> Use at your own risk; writing wrong values to a DSP can damage speakers. See
> the safety notes below.

## Install

```bash
pip install -e .
```

The core package needs only `pyserial` and `PyYAML`. Optional extras:

- `pip install -e '.[usb]'` — pyusb (raw-USB fallback)
- `pip install -e '.[encoding]'` — numpy (fixed-point helpers)

## Quickstart — CLI

```bash
atf-dsp-sdk list                       # enumerate serial ports; flag ATF devices
atf-dsp-sdk identify                   # model / firmware / current setup
atf-dsp-sdk get-setup
atf-dsp-sdk set-setup 3                # clean switch (no mute); add --dry-run to preview
atf-dsp-sdk read MOD_VOLUME_READBACK_VOLRB_READBACK_READBACKALGNEWSIGMA3005VALUE
atf-dsp-sdk write 0x11F8 00400000 --dry-run
atf-dsp-sdk write <PARAM_NAME> 00400000 --dry-run --model "MATCH M 5.4DSP"
```

## Quickstart — library

```python
from atf_dsp import Device

with Device.connect() as dev:                      # auto-detects by USB VID 0x2E4F
    print(dev.identify())
    dev.set_setup(2)                               # 1-based
    raw = dev.read_param("MOD_INPUTGAIN_ALG0_MUTE0")
```

## Channel model

A higher-level, data-driven abstraction over the raw params — inputs, virtual
(tuning) channels, outputs, and the two routing matrices. See
[docs/channels.md](docs/channels.md).

```python
from atf_dsp import Device

with Device.connect() as dev:
    dev.model.output("A").gain(-3).delay_ms(2.5)
    dev.model.output("B").eq_band(3, f=1000, Q=1.0, gain_db=-3.0, kind="peaking")
    dev.model.input("A").eq_band(0, f=120, Q=0.7, gain_db=+2.0)
    dev.model.virtual("Subwoofer 1").eq_band(3, f=80, Q=1.0, gain_db=+2.0)
```

Canonical channel identity is the **letter** (`Output A`, `Virtual A`,
`Input A`) — matching PC-Tool. Friendly names (`Front Left`) are per-setup
overlay labels only, never an addressing key. Counts and shapes come from
`tools/discover_channels.py` plus a per-model `.channels.yaml` overlay; nothing
about the channel topology is hardcoded.

Some encoders (delay / crossover / routing values, virtual gain/mute) are still
gated behind `unsafe=True` pending hardware readback confirmation. See
[docs/channels.md](docs/channels.md) for the current status matrix.

## Protocol & format documentation

The reverse-engineered protocol, on-wire framing, fixed-point encoders, the
`.at01`/`.pct6` project format, and the RTA / analyzer flow are documented in
`docs/`:

- [docs/conductor-protocol.md](docs/conductor-protocol.md) — command envelope,
  opcodes, evidence
- [docs/encoding.md](docs/encoding.md) — SigmaStudio 8.24/5.23 fixed-point
- [docs/channels.md](docs/channels.md) — channel model overlay
- [docs/pct6-format.md](docs/pct6-format.md) / [docs/pct6.md](docs/pct6.md) —
  vendor project format
- [docs/input-analyzer-re.md](docs/input-analyzer-re.md) — on-chip Input Signal
  Analyzer

## Supported models

Anything on the ACO platform (USB VID `0x2E4F`) should identify. Development
and hardware validation to date has been against **MATCH M 5.4DSP**. Other
MATCH / HELIX / BRAX models are supported by generating a param map from the
vendor `.at01` (see `tools/build_param_maps.py`); raw vendor files are never
committed.

## Tests

```bash
python -m pytest tests -q                                          # offline
ATF_DSP_SDK_HW=/dev/cu.usbmodemXXXX python -m pytest tests -q      # opt-in HW (read-only)
```

## Safety

This library can write arbitrary values to DSP parameters, including gain,
crossover, and routing. Wrong values can **damage speakers** or clip amp
output. Value encoders that have not been round-tripped against real hardware
are gated behind `unsafe=True`. Do not disable those gates casually. When
tuning by ear, keep amp gain low until you know what a change does.

## License

MIT — see [LICENSE](LICENSE).

## Disclaimer

**This is an independent, unofficial project. It is not affiliated with,
endorsed by, sponsored by, or supported by Audiotec Fischer GmbH.** It is built
exclusively for programmatic interoperability with a device the author lawfully
owns. "MATCH", "HELIX", "BRAX", and "DSP PC-Tool" are trademarks of their
respective owners and are used here only for identification. No vendor firmware,
installer, or project files are redistributed.
