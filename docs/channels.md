# Channel model (`acodsp.channels`)

The channel model raises the raw `MOD_..._ADDR` parameter surface to the PC-Tool's
mental model: physical **inputs**, **virtual** (tuning) channels, physical
**outputs**, and the two **routing matrices** (input→virtual and virtual→output).

It is **data-driven and model-agnostic**. Every count and shape comes from two
sources, never from hardcoded per-model logic:

1. **Structure** — `tools/discover_channels.discover()` reads the inflated `.at01`
   and derives input/output counts + letters, EQ blocks + band counts, gain/delay/
   mute/xover families, and the routing-matrix dimensions.
2. **Semantics** — `acodsp/data/<model>.channels.yaml` supplies the layer that can't
   be derived from names: human labels, which EQ block belongs to which channel
   class, virtual-channel identities, and routing orientation.

`ChannelModel.load()` combines the two. If the overlay is missing, the physical
layer is still built from discovery with generic labels.

## Usage

```python
from acodsp import Device

with Device.connect() as dev:                       # auto-detect by USB VID
    dev.model.output("A").gain(-3).delay_ms(2.5)     # fluent, chainable
    dev.model.output("B").eq_band(3, f=1000, Q=1.0, gain_db=-3.0, kind="peaking")  # full parametric
    dev.model.output("B").set_band_gain(16, -3.0)    # graphic: just the gain of band 16 (1 kHz)
    dev.model.output("B").band_frequency(16)         # -> 1000.0 (ISO centre for band 16)
    dev.model.output("A").crossover(hp=80, characteristic="butterworth", slope=12)
    dev.model.output("A").crossover(hp=80, lp=2500, characteristic="linkwitz_riley", slope=24)
    dev.model.input("A").eq_band(0, f=120, Q=0.7, gain_db=+2.0)
    dev.model.input("A").mute(True)

    dev.model.virtual("H").eq_band(3, f=80, Q=1.0, gain_db=+2.0)   # virtual EQ (CONFIRMED)
    dev.model.virtual("Subwoofer 1").eq_band(3, f=80, Q=1.0, gain_db=+2.0)  # same channel, by label

    # TWO routing matrices in the signal chain (Signal Management > Routing):
    i2v = dev.model.routing.input_to_virtual          # 9 virtual rows x 6 input-source cols
    i2v.read(dev); grid = i2v.as_grid()               # rows x cols of dB-linear gains (0x02)
    i2v.cell_param("A", "Input A")                    # -> ...ALG2VOL0000 (row=virtual, col=source)
    i2v.set("H", "Input A", -6.0, unsafe=True)        # Virtual H <- Input A (encoder gated)

    v2o = dev.model.routing.virtual_to_output         # 9 output rows x 9 virtual cols
    v2o.cell_param("A", "A")                          # -> ...ALG4VOL0000 (row=output, col=virtual)
    v2o.set("A", "A", 0.0)                             # routing = 8.24 linear gain (0 dB = 100%)
```

Offline / preview with `Device(link=Link(dry_run=True), ...)` — every write becomes a
built-but-unsent frame in `dev.link.sent` (reuses `Link.dry_run`).

### Setting EQ: graphic (by number) vs parametric (custom)

The output and virtual EQs are 30-band graphic EQs at fixed ISO ⅓-octave centres. Two ways to set a band:

- **Graphic, by number** — `channel.set_band_gain(i, gain_db, Q=None, kind="peaking")` sets *just the
  gain* of band `i`; the frequency comes from that band's canonical ISO centre and Q from the
  graphic default (both in the overlay's `graphic_eq`). `channel.band_frequency(i)` returns the centre.
  This is the "move slider N to X dB" case and needs no frequency/Q from the caller.
- **Parametric, custom** — `channel.eq_band(i, f, Q, gain_db, kind)` sets the band as an arbitrary
  biquad at your own frequency/Q.

`set_band_gain` recomputes the band's biquad at (ISO f, Q, gain) — so for a graphic band it changes
only the gain. It does **not** read back and preserve a *previously customised* f/Q (that needs a
hardware read-back); for that, use `eq_band` with the explicit values. Available on output/virtual
channels only — input EQ isn't a fixed-frequency graphic EQ, so use `eq_band` there. The default Q
(4.318, ⅓-oct constant-Q; PC-Tool "Fine EQ" showed ~4.3) is **inferred from the UI, not
hardware-confirmed** — override per call with `Q=` if a readback later shows different.

### Resolving names without writing

Every control exposes a `resolve_*` accessor returning a `ParamRef`
(`.name/.addr/.provisional`) or an `EqBandRef` (`.names/.base/.base_addr/.block/
.stage/.provisional`). This is how the engine turns a channel operation into a real
`.at01` param, and how tests assert correctness:

```python
dev.model.output("C").resolve_gain().name      # -> MOD_OUTPUTMUTE_ALG0_MUTE2 (gain/mute cell)
dev.model.output("A").resolve_delay().name     # -> MOD_DELAY__PHASE_SWITCH_DELAYA_DELAYAMT
dev.model.input("A").resolve_eq_band(3).names  # -> 5 INPUTEQA STAGE3 coeff params
dev.model.output("B").resolve_eq_band(2).base  # -> MOD_EQUALIZER_EQ1_2_ALG0_STAGE2_B2 (band<15 -> EQ1)
dev.model.output("B").resolve_eq_band(17).base # -> MOD_EQUALIZER_EQ2_2_ALG0_STAGE2_B2 (band>=15 -> EQ2)
dev.model.virtual("A").resolve_eq_band(0).base  # -> MOD_GLOBAL_EQ_FRONT_GLOBALEQFRONTL1_ALG0_STAGE0_B2
dev.model.virtual("A").resolve_eq_band(15).base # -> MOD_GLOBAL_EQ_FRONT_GLOBALEQFRONTL2_ALG0_STAGE0_B2 (2nd sub-block)
dev.model.virtual("H").resolve_eq_band(0).base  # -> MOD_GLOBAL_EQ_SUB_GLOBALEQFRONTL1_ALG0_STAGE0_B2 (SUB naming quirk)
```

## What is CONFIRMED vs PROVISIONAL

Confirmed (`.provisional is False`):

- Output **gain / mute / delay / crossover by physical letter**.
- **Input EQ** and **input mute**.
- **Output EQ = EQ1 + EQ2 cascaded** — a 30-band graphic/parametric EQ (ISO ⅓-oct
  25 Hz–20 kHz). Confirmed from the PC-Tool "Outputs → Equalization" screenshot
  (2026-08-26). Band `i`: 0–14 → EQ1 stage `i`, 15–30 → EQ2 stage `i−15`; bands 0–29
  are the 30 UI graphic bands, band 30 is an extra physical stage (likely the "Fine EQ").
- **Channel naming**: the canonical `.label` is the **letter pointer** — `"Output A"`,
  `"Virtual A"`, `"Input A"` — matching the PC-Tool's `[A]…[I]`. This is stable per device and
  is what code should surface. Friendly names (e.g. "Front Left", "Subwoofer 1") are **per-setup,
  not hardware constants** (they live in the `.pct6` setup, not the device files — there are zero
  name params in any model's `.at01`), so they are exposed only as an **optional `.setup_label`**
  populated from the illustrative "Demo 1" overlay, and are never used as an addressing key. A
  channel can still be *selected* by that friendly name for convenience (`virtual("Subwoofer 1")`).
- **Virtual-channel EQ** — the PC-Tool "Virtual" tab (screenshot). There are **9 virtual
  channels A–I**: A Front L, B Front R, C Rear L, D Rear R, E Front Center, F Rear Center,
  **G "Pass Through 1"**, H Subwoofer 1, I Subwoofer 2. **Virtual G is a pass-through with
  NO EQ** (the overlay marks it `pass_through: true` and omits `eq_subblocks`); it exists in
  the routing matrices but not in the Virtual EQ tab, so `virtual("G").eq_band(...)` and
  `resolve_eq_band(...)` raise `ChannelModelError("pass-through channel 'G' has no EQ")` and
  `virtual("G").eq_band_count == 0`. The other **8 channels each have a 30-band EQ =
  two consecutive 15-stage `MOD_GLOBAL_EQ` sub-blocks** (bands 0–14 → first sub-block,
  15–29 → second), addressed via the `templates.global_eq` template and the per-channel
  `virtual_channels.channels.<key>.eq_subblocks` list. Vendor naming quirk: virtual **H**
  (Subwoofer 1) has its first sub-block spelled `SUB_GLOBALEQFRONTL1` (not `…SUBL1`).
  Select a channel by **letter key** (`virtual("H")`) or **label**
  (`virtual("Subwoofer 1")`, case-insensitive). The module/addressing and channel
  identities are confirmed, so `resolve_eq_band(...).provisional is False`.

> Note: **all value encoders are now hardware-confirmed** (gain, mute, delay, EQ, crossover,
> routing; fs=48 kHz) — no `unsafe=True` needed for the typed API. Cross-referenced against the
> PC-Tool 2026-08-26.

Remaining small unknowns (reads are always safe; mis-mapped ops fail loudly, not silently):

- **Virtual-EQ band ordering** — which sub-block is the low-frequency half of the 30-band
  span is not yet verified; the engine follows the overlay's listed `eq_subblocks` order.
- **Crossover** covers **Butterworth / Linkwitz-Riley at 12 & 24 dB** (hardware-confirmed);
  Bessel and 6/18 dB slopes aren't mapped yet, so `crossover()` raises `ChannelModelError` for them.

Channel gain/mute (hardware-confirmed 2026-08-26): output gain/level = the **`MOD_OUTPUTMUTE`**
gain/mute cell (`output.gain()` → `..._MUTE{index}`), virtual gain = **`MOD_VCPMUTE`**
(`virtual.gain()` → `..._MUTE{index}`). Gain and mute **share the cell** (0 = mute, else the gain),
so `mute(False)` writes 0 dB — set a level with `gain()` after unmuting. (`MOD_GAIN_GAIN{A..I}` is
*not* the channel gain — an earlier bug pointed `output.gain()` there.)

## Routing matrices (there are TWO)

The MATCH signal chain has **two** routing matrices, both exposed on
`dev.model.routing` (a `Routing` container). Each cell is an **8.24 linear-gain mix
coefficient**; the param name is `…VOL{row}{col}` with **two zero-padded digits each**,
**row = destination, col = source**. The value is a linear gain where **UI % = linear × 100**
(100% = `0x01000000`, 50% = `0x00800000` = −6 dB). Hardware-confirmed 2026-08-26; `set()` needs
no `unsafe=True`.

| Matrix | Module template | Dims | Rows (dest) | Cols (source) | Orientation |
| --- | --- | --- | --- | --- | --- |
| `input_to_virtual` ("Main/Digital to Virtual Routing") | `MOD_MAINMATRIX_ALG0_NXNMIXS3004P6ALG2VOL{row}{col}` | 9 × 6 | virtual A–I (0–8) | input source (0–5) | **CONFIRMED** (screenshot) |
| `virtual_to_output` ("Virtual to Output Routing") | `MOD_NXMLINEAR1_ALG0_NXNMIXS3004P6ALG4VOL{row}{col}` | 9 × 9 | output A–I (0–8) | virtual A–I (0–8) | **CONFIRMED** (screenshot: outputs=dest/right, virtual=src/left) |

`input_to_virtual` is **confirmed** from the "Main → Virtual" screenshot: Virtual A (row 0)
← Input A (col 0) @ 100 %; Virtual H/Sub 1 ← Front L 50 % + Front R 50 %. The **6 input
source columns** are, in order: `0` Input A, `1` Input B, `2` Input C, `3` Input D,
`4` Digital In L, `5` Digital In R (from the overlay `sources` labels).

Each matrix exposes `.dims`, `.rows_kind`/`.cols_kind` (`'virtual'|'input'|'output'`),
`.confirmed`, `.sources`, `.cell_param(row_sel, col_sel)` (accepting an int index **or** a
channel letter/label mapped to the right axis), `.read(device)` / `.as_grid()`, and
`.set(row_sel, col_sel, gain_db, unsafe=False)`. The top-level `dev.model.routing`
keeps a small deprecated shim (`.read`/`.as_grid`/`.cell_param`/`.route`/`.rows`/`.cols`/
`.provisional`) that delegates to `virtual_to_output`.

## Correcting the provisional guesses (one-line overlay edits)

All of the above live in `acodsp/data/MatchM54DSP.channels.yaml`. The engine reads
these — nothing is hardcoded — so a PC-Tool screenshot is corrected by editing the
overlay only:

| To change… | Edit this overlay key |
| --- | --- |
| EQ-block → channel-class assignment | `eq_blocks.<BLOCK>.assigned_to` (`output` \| `virtual` \| `virtual_role`) — a class spanning two blocks (e.g. output = EQ1+EQ2) cascades `EQ1[0..14]` then `EQ2[0..15]` automatically |
| Virtual-channel EQ sub-blocks / band ordering | `virtual_channels.channels.<key>.eq_subblocks` (ordered `[low-half, high-half]`) + `templates.global_eq` |
| Virtual-channel keys / illustrative setup names | `virtual_channels.channels.<key>.label` (exposed as `.setup_label`) |
| Pass-through virtual channel (no EQ) | `virtual_channels.channels.<key>.pass_through: true` (or omit `eq_subblocks`) |
| Routing matrix axes / dims / orientation flag | `routing.matrices.<name>.{rows,cols,dims,confirmed,sources}` |
| Input / output illustrative setup names (`.setup_label`) | `inputs.labels`, `outputs.demo_setup_labels` |
| Name templates (for a different model's naming) | `templates.*`, `routing.matrices.<name>.template` |

The band→(block, stage) mapping is built purely from `eq_blocks.*.assigned_to` + the
discovered band counts, so reassigning a block is a config change with **no code edit**
(see `tests/test_channels.py::test_flipping_overlay_changes_resolution`).

## Current MATCH M 5.4DSP mapping

Discovered: `inputs=4 (A–D)`, `outputs=9 (A–I)`, `EQ1=9 inst × 15 bands`,
`EQ2=9 inst × 16 bands`; routing = `input_to_virtual` (9 × 6) + `virtual_to_output`
(9 × 9).

**Confirmed** (screenshots 2026-08-26): output EQ = EQ1+EQ2 (30-band graphic EQ, ISO
centers in `output_eq_frequencies_hz`); canonical labels are letters (A→I); the "Demo 1"
`setup_label`s are Front L/R, Rear L/R, Subwoofer, Line Out 1–4 (illustrative, per-setup);
**virtual channels** = 9 (A–I) — 8 with a 30-band EQ over 2×15
`GLOBAL_EQ` sub-blocks (incl. the `SUB_GLOBALEQFRONTL1` naming quirk on H) plus **G "Pass
Through 1" with no EQ**; **both routing matrices** confirmed — `input_to_virtual`
(row = virtual, col = input source; 6-source order A–D, Digital L/R) and `virtual_to_output`
(row = output, col = virtual; outputs = destinations). Channel gain/mute cells
(`OUTPUTMUTE`/`VCPMUTE`), the routing encoder (8.24 linear %), and the crossover table
(Butterworth/LR × 12/24 dB) are all hardware-confirmed. **Still open**: virtual-EQ band
ordering (which sub-block is the low half), Bessel/6/18 dB crossovers, and input-channel
labels/gain (the PC-Tool "Input" tab).
