# Hardware validation runbook — proving atf_dsp SET/READ against the DSP PC-Tool

This is the procedure that turns "the offline suite is green" into "the amp agrees, and the
PC-Tool agrees." The harness lives in `atf_dsp/validate.py`; the on-amp gate is `ATF_DSP_SDK_HW`.

## Why two directions (read this first)

A naive "set via SDK → read via SDK → assert" loop is **partially circular**: it uses *our*
encoders on both ends. It proves writes persist, that encode/decode are inverse, and that the
firmware SafeLoad path works — but it does **not** prove a cell is the *semantically correct* one,
nor that the PC-Tool agrees on what a value means. The **PC-Tool is the only independent oracle**.
So we validate in two directions:

| Direction | What runs | What it proves | Circularity boundary |
|-----------|-----------|----------------|----------------------|
| **B — SDK writes → SDK reads** | `exercise_all(device)` | writes persist · encode/decode inverse · SafeLoad works · same on real hw | same encoders both ends → says nothing about *semantic* correctness or PC-Tool agreement |
| **A — PC-Tool writes → SDK reads** | `assert_matches_pct6(pct6, vector)` | our `.pct6`/`from_device` DECODE matches the PC-Tool's ENCODE for every DSP-backed field | independent — the PC-Tool produced the bytes |

Direction B runs **offline** against the memory-backed dry-run device *and* unchanged on real
hardware. Direction A needs a human at the PC-Tool once to produce the oracle file.

## Tolerances (never compare exact XML/text)

Every comparison is tolerance-aware because the DSP round-trip is lossy by construction:

- **EQ** is stored as **biquad coefficients**; f/Q/gain are *recovered* from them (rounding).
- **Frequencies/gains** quantize through 8.24 fixed point.
- **Delay** is an **integer sample** count; **crossover corner** is recovered from the pole coeffs.

`DEFAULT_TOL` (`atf_dsp.validate.Tolerance`): **±0.5 Hz or ±1 %** (whichever larger) on frequency,
**±0.02** on Q, **±0.1 dB** on gain, **±1 sample** on delay, **±0.1 dB** on a routing mix cell.
Only **DSP-backed** fields are compared. The **lossy `.pct6` fields are skipped/annotated, never
asserted**: channel role code `CN`, channel display names, `EqBy`/`CE`/UI flags, routing
`AOM`/`OFFS` metadata (these are PC-Tool setup concepts, not DSP RAM params).

## The test vector

`build_test_vector(model)` is **deterministic** and gives **every channel a distinct value per
control**, so a mis-mapped cell (wrong channel or wrong band) is caught by a value mismatch, not
just a shape check. Coverage:

- **per output** — gain (dB), mute (a spread of channels muted), delay (ms → distinct samples),
  three distinct EQ bands (distinct f/Q/gain), and a crossover (HP-only / LP-only / both rotate so
  *off* sections are exercised; characteristic + slope vary across the confirmed table);
- **per virtual** — gain, mute, three EQ bands (pass-through virtuals get gain/mute only);
- **per input** — EQ bands;
- **a spread of cells in BOTH routing matrices** (`input_to_virtual` and `virtual_to_output`).

## Procedure

### 0. Prerequisites
- Amp connected; find the port (macOS: `ls /dev/cu.usbmodem*`).
- **There is no confirmed SDK "blank/new setup" primitive.** Start from a PC-Tool baseline: open a
  known-good setup in the PC-Tool and push it to the amp, so you begin from a defined state. Do not
  assume the amp is zeroed.
- Every hardware write path snapshots + restores. Never use the `0x29`/remote group (it mutes) —
  setup switching is `0x1F` only.

### 1. Direction B — on the amp (SDK writes → SDK reads)
```bash
ATF_DSP_SDK_HW=/dev/cu.usbmodemXXXX ATF_DSP_SDK_HW_WRITE=1 \
  .venv/bin/python -m pytest atf_dsp_control/tests/test_validate.py -k on_real_hardware -q
```
`exercise_all(device, snapshot=True)` snapshots every touched cell, writes the whole vector,
reads each back within tolerance, then **restores** — the amp is left exactly as found. A green run
proves persistence + encode/decode + SafeLoad on real silicon. It does **not** prove semantics —
that is Direction A.

Or interactively, leaving the setup on the amp for the PC-Tool to read:
```python
from atf_dsp import Device, build_test_vector, exercise_all
dev = Device.connect(port="/dev/cu.usbmodemXXXX")
vec = build_test_vector(dev.model)
rep = exercise_all(dev, vec, snapshot=False)   # PERSIST so the PC-Tool can read it back
print(rep.summary())
```

### 2. Direction A — the PC-Tool as the oracle (FILE-BASED ONLY)
**The PC-Tool has NO "read from amp" feature.** It cannot pull live device RAM into the GUI — it
only *loads* a `.pct6` and *saves* a `.pct6` (and on connect it *pushes* its in-GUI setup to the
amp, which is why connecting mid-test audibly overwrites SDK-written state). This is the whole
reason the `.pct6` machinery exists, and why the oracle validation runs in **PC-Tool demo mode, no
amp** — it is entirely file-to-file. There is exactly one oracle path:

**PC-Tool-authored `.pct6`, SDK decodes (fully independent):**
1. In the PC-Tool (demo mode is fine — no amp needed), **build a known varied setup by hand**:
   per output the gain, mute, delay, three EQ bands (frequency/Q/gain), and the crossover (corner,
   characteristic, slope); per virtual the gain/mute + EQ bands; input EQ bands; routing cells.
2. **File → Save As** → `oracle.pct6`.
3. Assert our decode matches the values you entered:
   ```python
   from atf_dsp import assert_matches_pct6, build_test_vector, Device
   vec = build_test_vector(Device.connect(port="…", model="MATCH M 5.4DSP").model)
   print(assert_matches_pct6("oracle.pct6", vec).summary())
   ```
   Green means the PC-Tool ENCODED the same values → our decode is correct. (To exercise the exact
   `build_test_vector` numbers, read them off `build_test_vector(model)` and enter those by hand.)

There is **no SDK-writes → PC-Tool-reads path** — the PC-Tool can't read the amp. The amp's own
validation is **Direction B** (SDK write → SDK read, above) plus **physical measurement** (e.g. a
UMIK-1 cancellation test for polarity); the PC-Tool only ever validates the `.pct6` file format.

### 3. Read the report
`Report.summary()` prints `passed/total` and lists every failing control with `expected` vs
`actual`, plus the skipped lossy fields. `Report.raise_for_status()` turns a failure into an
`AssertionError`. Non-empty `failures()` with a concrete value delta points straight at the
mis-mapped cell or a wrong encoder.

## Crossover-corner recovery (confidence)

`from_device` and `OutputChannel.recover_crossover()` recover output crossovers from the FILTERS
biquad stages (HP = stages [0,1], LP = [3,2]):

- **Corner frequency + Q** — the **exact analytic inverse** of the written RBJ pole coefficients
  (`encoding.lphp_corner_from_coeffs`); the LP and HP share pole coefficients, so which is which
  comes from the stage slot, not the coefficients.
- **Slope** — from the **count of active (non-bypass) stages** in the pair: 1 stage = 12 dB/oct,
  2 = 24. Trustworthy.
- **Characteristic** (Butterworth vs Linkwitz-Riley) — **inferred, best-effort**, by matching the
  corner-stage Q against the confirmed `_XOVER_Q` table. With the SDK's own write order the four
  supported combos have distinct (slope, Q) pairs, so it is reliable *for those*; anything outside
  the table returns `None` rather than fabricating precision. Recovered fields are marked
  `approximate=True`. The harness tolerance-compares corner + slope (reliable) and does a
  best-effort characteristic check.

## Still-provisional encoders to confirm this way

These ride paths that are `unsafe=True`-gated and/or provisional; the two-direction procedure is
how they get confirmed on real hardware:

- **delay** — integer-sample encoding at fs = 48 kHz (gated `unsafe=True`).
- **crossover** — per-slope stage mapping + characteristic (gated `unsafe=True`).
- **routing** — mix-cell addressing / orientation in both matrices (gated `unsafe=True`).
- **polarity** — the `INV{letter}` cell is confirmed but its **±1.0 sign-multiplier encoding is
  PROVISIONAL/unvalidated**; not in the default vector — readback-verify separately on an amp.
- **ISA** (input signal analysis / source config) — separate and lower priority; not in scope here.

## What the offline tests CANNOT do (and this session must)

The offline suite runs Direction B against a *memory-backed simulator* — it proves the plumbing,
never the physics. Only the amp + PC-Tool session can establish:

1. that a written cell actually **takes effect on the audio** and is the **semantically correct**
   parameter (Direction B on the simulator can't — the sim just echoes bytes back);
2. that the **PC-Tool agrees** on encode/decode (Direction A — the sim is not an independent
   oracle);
3. that the **gated/provisional encoders** (delay, crossover, routing, polarity) behave as assumed
   on real silicon;
4. the real **runtime SafeLoad window base** and glitch-free application on hardware.
