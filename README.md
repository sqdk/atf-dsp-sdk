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

## Overview

`atf-dsp-sdk` talks to the amplifier's DSP over its USB-CDC serial link and,
roughly bottom to top, provides:

- **Transport & protocol** — the reverse-engineered ACO command framing
  (checksummed `B`/`C` envelopes), parameter read/write, and clean setup
  switching (`0x1F`), plus a dry-run mode that builds frames without touching
  hardware.
- **Parameter maps** — per-model name↔address maps generated from the vendor
  `.at01` device files, so parameters are addressable by symbolic name.
- **Fixed-point encoders** — pure functions for the ADAU1452's native 8.24
  format: gain (dB), mute, integer-sample delay, RBJ biquad EQ bands, and
  crossover biquad chains.
- **Typed control surface** — `gain / mute / delay / eq_band / crossover /
  routing / polarity`, written glitch-free through the firmware's own SafeLoad
  path.
- **Channel model** — a data-driven abstraction over inputs, virtual (tuning)
  channels, outputs, and the two routing matrices, addressed by PC-Tool letter
  identity; topology is discovered, never hardcoded.
- **Input Signal Analyzer / RTA** — read the on-chip per-channel level and
  spectrum readback cells.
- **Vendor project files** — parse and apply `.pct6` / `.at01` setups
  (`apply_setup`), plus a validation harness that round-trips SDK state against a
  PC-Tool-authored `.pct6` as an independent oracle.
- **CLI** — `list`, `identify`, `get-setup`, `set-setup`, `read`, `write`.

Everything writable goes through a dry-run / `unsafe` gating model, so nothing
unvalidated writes to an amp silently.

## Install

```bash
pip install -e .
```

The core package needs only `pyserial` and `PyYAML`. For development:

- `pip install -e '.[dev]'` — pytest (test suite)

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

All typed value encoders — gain, mute, polarity, delay, EQ (peaking + shelves),
crossover, phase, and routing — are hardware-confirmed against the MATCH M 5.4DSP
and need no `unsafe=True`. Polarity is the **sign of the output gain cell**
(`output.polarity()` / `output.gain(db, inverted=True)` / `output.is_inverted()`),
not a separate switch. See [docs/channels.md](docs/channels.md) for the full
status matrix and [Hardware validation](#hardware-validation) below.

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

## Device support

The ~35 models on the Audiotec Fischer **ACO platform** (USB VID `0x2E4F`) share
a single firmware; only the USB PID and the per-model parameter map differ. The
PID→model table in [`atf_dsp/models.py`](atf_dsp/models.py) covers the current
**MATCH / HELIX / BRAX** range (from the MATCH UP / M series through HELIX DSP /
V-series to BRAX DSP), so any ACO device should enumerate and identify.

Parameter maps are **generated**, not hand-written. Each is derived from the
model's `.at01` **device file** — the SigmaStudio parameter export (name → DSP
address) that ships inside the DSP PC-Tool install under `.../app/deviceFiles/`,
one per model. A single PC-Tool installation therefore already contains the
device file for *every* ACO model, whether or not you own that amp. (An `.at01`
is not a saved tune — that's a **`.pct6`** setup/project file, handled
separately.) `tools/build_param_maps.py` inflates an `.at01` and writes the
`{name: address}` JSON; **only the generated JSON is committed, never the raw
`.at01`.**

The repository ships a generated map for the **MATCH M 5.4DSP** only — the model
that has been hardware-validated. Maps for the other ACO models can be generated
identically from the same PC-Tool install (the shipped `model_topology.json`,
which covers 32 models, was itself built by scanning those device files). They
are not shipped by default only to keep the package small and to avoid implying
validated support for models exercised on paper alone — generating them is no
different from the map that already ships.

### Adding another model

1. Find the model's `.at01` in your DSP PC-Tool install
   (`.../app/deviceFiles/`), e.g. `HelixDSP3.at01`. The basename must match the
   one mapped for that model in [`atf_dsp/models.py`](atf_dsp/models.py)
   (`MODEL_AT01`).
2. Generate the parameter map:
   ```bash
   python tools/build_param_maps.py /path/to/HelixDSP3.at01 --out atf_dsp/data
   # …or build every model found in the folder at once:
   python tools/build_param_maps.py --scan /path/to/deviceFiles --out atf_dsp/data
   ```
   This writes `atf_dsp/data/<basename>.json`.
3. Raw parameter read/write, the protocol layer, and the CLI now work for that
   model (pass `--model "<name>"`, or let `identify` detect it by USB PID).
4. For the high-level **channel model**, add a per-model semantic overlay
   `atf_dsp/data/<basename>.channels.yaml` (channel labels, EQ-block roles,
   virtual-channel identities, routing orientation). Without it the physical
   layer still loads — you get raw addressing by name — but the labelled channel
   semantics won't be right. Use the shipped `MatchM54DSP.channels.yaml` as a
   template.

> **Not hardware-validated beyond the MATCH M 5.4DSP.** Every ACO model shares
> one firmware and the same encoders, so a generated map is *expected* to work —
> but it is unverified. Read back what you write, keep amp gain low, and mind the
> encoder gates, which exist precisely for untested paths.

## Hardware validation

Development, and all hardware validation to date, has been against a **MATCH
M 5.4DSP** (bench sessions 2026-08-26 → 08-31). Because the DSP round-trip is lossy by
construction, validation runs in two directions with tolerance-aware
comparisons — the runbook is
[docs/hardware-validation.md](docs/hardware-validation.md):

- **SDK writes → SDK reads** on the real amp — proves writes persist, that
  encode/decode are inverse, and that the firmware SafeLoad path works on
  silicon.
- **PC-Tool writes → SDK reads** from a PC-Tool-authored `.pct6` — the PC-Tool
  is the only independent oracle, so this proves the encoders are *semantically*
  correct, not merely self-consistent.

Confirmed on hardware (no `unsafe=True` required):

| Area | Confirmed |
|------|-----------|
| 8.24 fixed-point · gain (dB) · mute | 2026-08-26 |
| Polarity — the **sign** of the output gain cell | 2026-08-30 |
| Delay (integer samples, fs = 48 kHz) | 2026-08-26 |
| EQ — RBJ biquad peaking **and** low/high shelves | peaking 2026-08-26 · shelves 2026-08-30 |
| Crossover — Butterworth / Linkwitz-Riley at 12 / 24 / 36 dB | 12·24 2026-08-26 · 36 dB + stage-map fix 2026-08-30 |
| Phase — 2nd-order all-pass section (0–360°) | 2026-08-30 |
| Whole-EQ bypass | 2026-08-31 |
| Routing (both matrices; linear mix gains) | 2026-08-26 |

Note: an earlier crossover stage mapping (`LP = [3, 2]`) was wrong and was
corrected to `HP = [0,1,2]` / `LP = [3,4,5]` against the PC-Tool oracle; it had
only ever "passed" because SDK-writes→SDK-reads is circular.

Not yet fully validated:

- **Crossover** — the 36 dB *Linkwitz-Riley* Q's were captured off the amp, but
  the 36 dB *Butterworth* Q's use the standard 6th-order values and have not yet
  been oracle-captured; **Bessel** and 6 / 18 dB slopes are not mapped at all.
- **Input Signal Analyzer** — the exact readback cell set and polling cadence
  still need a live capture to confirm.

Only the MATCH M 5.4DSP has been exercised on real silicon; other ACO models
share the firmware and encoders but have not been individually hardware-tested.

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

## Built with AI assistance

This project was built **almost entirely with AI assistance**. The protocol
reverse-engineering, the encoders, the channel model, the test suite, and this
documentation were largely produced by working with AI coding tools. The human
author's role has been to direct that work, provide the hardware, and — crucially
— perform the **on-amp validation the AI cannot**: every "confirmed" claim in
[Hardware validation](#hardware-validation) was checked against a real MATCH
M 5.4DSP and the vendor PC-Tool.

Read the code with that in mind: it is functional and hardware-validated where
noted, but review it before trusting it near equipment you care about, and
respect the `unsafe=True` gates on anything not yet confirmed.

## License

MIT — see [LICENSE](LICENSE).

## Disclaimer

**This is an independent, unofficial project. It is not affiliated with,
endorsed by, sponsored by, or supported by Audiotec Fischer GmbH.** It is built
exclusively for programmatic interoperability with a device the author lawfully
owns. "MATCH", "HELIX", "BRAX", and "DSP PC-Tool" are trademarks of their
respective owners and are used here only for identification. No vendor firmware,
installer, or project files are redistributed.
