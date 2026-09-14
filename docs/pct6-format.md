# `.pct6` / `.afpx` setup-file format (DSP PC-Tool 6)

Reverse-engineered from `ATF_DSP_PC-TOOL_6.exe` and **proven** by decrypting the shipped sample
`setups/UP_8BMW_PP-BMW1_7Hifi_Basic.pct6` → 54,631 bytes of well-formed XML (exact declared-length
match). (There is **no AES** — an earlier guess based on entropy +
block-alignment was wrong; the flat entropy is just zlib-under-XOR.)

## The transform

```
plaintext_xml = qUncompress( XOR(whole_file_bytes, KEY) )
```

- **Obfuscation:** repeating-key XOR (stream): `out[i] = data[i] ^ KEY[i % len(KEY)]`. Applied to the
  *entire* file — there is **no header, IV, salt, or padding**. The apparent `"AT\x93\x31"` magic is
  an artifact: it's the qCompress 4-byte length (`0x0000D567`) XOR the key.
  - XOR routine: `FUN_00609680 @ 0x00609680`.
- **KEY:** an embedded ASCII literal, chosen by the format-detector `FUN_005ff470 @ 0x005ff470`,
  which tries variants until `qUncompress` succeeds:
  | Key | Meaning | Notes |
  |---|---|---|
  | `ATFV6` | standard `.pct6` | **the sample's variant**; literal @ `0x028898a0` |
  | `ATFV6P` | password-protected `.pct6` | NOT fully RE'd; involves `QCryptographicHash`; irrelevant unless opening pw-protected files (literal @ `0x028898a8`) |
  | `ATF` | legacy `.afpx` | literal @ `0x028551dc` (`41 54 46 00`) |

  Plain `qCompress` (no XOR) and uncompressed paths also exist. Keys are compile-time constants
  (not per-version / not runtime-assembled).
- **Compression:** Qt `qUncompress` = **4-byte big-endian uncompressed length + a raw zlib stream**
  (magic `78 da`). Called via `PTR_qUncompress_0079c810`.

Writing is the exact inverse: `XOR(qCompress(xml), KEY)` — no auth tag to reproduce.

Decryptor: `scratchpad/pct6_decrypt.py` (stdlib only; auto-detects all variants).

## Plaintext container

Qt `QDomDocument` XML, parsed by `FUN_00615090` (references `"afpxFileType:"`). Root `<ATF>`:

- **`<ATF>` attributes** (metadata): `Dev` = the PC-Tool **INTERNAL device-type id** (its own
  model enum, e.g. `29` for the M 5.4DSP, `50` for the UP 8BMW) — **NOT the USB PID** (`0x2008`
  for the M 5.4DSP): the two are different number spaces (see `atf_dsp.models.DEV_IDS` for the
  internal ids vs `PID_MODELS` for the USB PIDs). `V` = tool version (e.g. `6.03.03`),
  `OUTS` = output-channel count, `INS` = input-channel count, `FN` = original path, plus
  `VCO/VM/IOR/AV/IGL/IGM/FV/TM/ICT/...` flags.
- **`<OC>`** — output channels (`OUTS` of them). Attributes incl. `ON` (slot), `CE` (enabled),
  `GD`, `LG`, `EqBy` (EQ bypass), `CN` (role), `MT` (**assigned/populated, NOT mute**), `Finit`,
  `CINV`, `DG` — see the full populated-channel schema table below. Contains:
  - **`<Fil>`** — EQ/filter bands, **listed low→high frequency** (first band `F="25.00"`). Attributes:
    `F` = frequency (Hz), `G` = gain (dB), `Q` = Q (graphic default `4.3`), `T` = filter type,
    `FilBy` = bypass, `I` = index, `FilBBR`. (648 `<Fil>` total in the sample.)
- **`<IC>`** — input channels (`INS`).
- **`<DC>`**, **`<Vol>` / `<VOL>`**, **`<SW>`**, **`<R>`** (routes), **`<TC>`**, and a trailing
  **`<ATFCOND>` / `<GLB>`** (CONDUCTOR / global) block.

### Confirmations this gives us (for free, no hardware)
- **Graphic-EQ Q = 4.3** — matches the value inferred from the UI + hardware cross-ref.
- **EQ band ordering** — `<Fil>` are low→high (band 0 = 25 Hz), pinning the ordering the channel
  model left provisional.
- **Friendly channel names / labels, input labels, per-band values** are all present in plaintext →
  Phase C parses them into a `Setup` model (resolves the parked naming problem).

## Status
Phase A (scheme) + Phase B (decrypt + container) — **CONFIRMED** (self-proving decrypt).
Phase C (XML → `Setup` model) + a safe slice of Phase D (encrypt/save round-trip) — **DONE**
in `atf_dsp_control/atf_dsp/pct6.py` (`Setup.load/save`, `Device.apply_setup` dry-run;
`<Fil T>` codes 1/17=peaking, 9=lowpass, 10=highpass — the latter two inferred from the
HPi/LPi index correspondence). See `atf_dsp_control/docs/pct6.md` for the model + usage.
Phase D **CONFIRMED end-to-end (2026-08-27)**: a library-edited `.pct6` (changed a note + one
EQ band gain, re-encrypted) **loaded in the actual PC-Tool with the edits visible** — the tool
accepts library-written files.

## `<OC>` split: virtual vs physical output (RESOLVED 2026-08-27)
`<OC>` holds BOTH the virtual (tuning) channels **and** the physical outputs, virtuals first.
Discriminate by **crossover presence**: physical outputs carry `HPi`/`LPi` (crossover filter
indices); virtual channels have neither. This is NOT `OUTS/2` — confirmed on two files:
- M 5.4DSP (`Dev=29`): `OUTS=18` = **9 virtual + 9 output**.
- UP 8BMW (`Dev=50`): `OUTS=20` = **11 virtual + 9 output**.
Each group is re-indexed 0.. → A.. . `<IC>` (inputs) use `IN` (0..3 = Main A–D, 4..5 = Digital L/R).
`Setup` exposes `.virtuals`, `.outputs`, `.inputs`; `apply_setup` maps them to
`model.virtual`/`model.output`/`model.input`.

## `<OC>` / `<IC>` populated-channel schema (RESOLVED 2026-08-28, real M 5.4DSP + PC-Tool)
Verified attribute-by-attribute against a user's real `new-10-new-eq-2.pct6` (`Dev=29`) with the
values cross-checked in the PC-Tool UI. This is the schema `Setup.from_device` now emits so the
PC-Tool renders our exported gain/delay (previously it hid them, treating our channels as unset).

A **populated `<OC>`** (child order `Fil*`, `Vol`, `T`, `CHS`):

| attr | meaning | confidence |
|------|---------|-----------|
| `ON` | slot index in the OUTS space — **virtuals `0..N-1`, physical outputs `N..` where `N = #virtuals`** (M54: virtuals 0–8, outputs 9–17) | CONFIRMED |
| `MT` | **ASSIGNED/populated flag — NOT mute.** Real assigned channels (`CN≠0`) carry `MT=1`; unassigned (`CN=0`) `MT=0`. Inputs are always `MT=0`. `MT=0` on a populated channel is why the PC-Tool hid our gain/delay. | CONFIRMED (invariant holds on both sample files) |
| `CN` | role code (0 = "Not assigned"); a PC-Tool setup concept, **not** DSP RAM → `from_device` leaves it `0` | CONFIRMED |
| `CE` | channel enabled (`1`) | CONFIRMED |
| `Finit` | number of graphic/parametric `<Fil>` bands, **excluding** the crossover filters (M54 output/virtual = 30, analog input = 15, digital input = 0) | CONFIRMED |
| `LG` | link group (`0` = none); membership not recoverable from RAM → emit `0` | INFERRED |
| `CINV` | output **polarity** (`0` = normal, `1` = inverted). `from_device` reads each output's DSP polarity cell (`MOD_DELAY__PHASE_SWITCH_INV{letter}`, a ±1.0 8.24 sign multiplier — negative = inverted) and emits `1`/`0` accordingly (was hard-coded `0`). | CONFIRMED (`CINV` field); the ±1.0 silicon encoding is PROVISIONAL/gated |
| `DG`, `GD` | DSP/group flags, `0` in every real output | GUESSED meaning, `0` CONFIRMED |
| `EqBy` | EQ bypass (`0` = off) | CONFIRMED |
| `HPi`/`LPi` | crossover-filter indices (physical outputs only; out-of-range = section off) | CONFIRMED |

Children:
- **`<Vol L=…>`** — gain as a **linear** multiplier: `gain_db = 20·log10(L)` (0 dB → `L=1`, −6 → `0.501`).
  **MUTE** shares this cell with gain: `L ≤ 0` == muted (mute is *not* a separate attribute; it is
  the same DSP cell as gain, so `.pct6` represents it as `L=0`). `.mute` is derived from `L`, `.assigned` from `MT`.
- **`<T PM=1 P=0 T=<samples>>`** — output **delay** as an integer **sample count @48 kHz**: `ms = T/48`
  (real examples: `T=159`→3.31 ms, `108`→2.25, `146`→3.04, `235`→4.90, `0`→0). The delay value is in
  the **`T` attribute**, present even at 0 delay. (Our historical bug emitted `<T D=…>` — a nonexistent
  attribute the PC-Tool ignored, so delay showed unset.)
- **`<CHS LATERAL=0 LONG=0>`** — delay-mode sibling (lateral/longitudinal). `PM=2` channels use
  `<CHS PB=… IDX=…>` instead; meanings not needed for `from_device` (emit the `LATERAL/LONG` form).

A **populated `<IC>`** input (child order `Fil*`, `Vol`, `T`, `CHS`) uses `IN`, `CN`, `MT=0`, `Finit`,
`CINV`, `HPi`/`LPi` (one past the band list = crossover off), `EqBy`; its `<T>` carries only `T=0`.
**Digital inputs** (no tunable DSP params) are emitted **flat** — just `<CHS LATERAL=… SH=3>`, no
`<Vol>`/`<T>`/`<Fil>` — matching the vendor file.

## `CN` = channel name/role enum → friendly names (RESOLVED 2026-08-27)
Each channel's `CN` is a role code (same value threads the input→virtual→output chain, e.g. `CN=1`
= Front Left). The name isn't a stored string — `CN` keys a `std::map<int, ChannelSetup>` in the exe
(builder `FUN_005f0410`, lookup `0x5ef5d0`) whose descriptor is composed as
`"[type] [position] [side] [band] [number]"` (composer `0x5f3e30`) from `tr()` keys. The full
**53-entry `CN`→name table** is recovered into `atf_dsp/pct6.py` (`CN_NAMES`), VALIDATED against the
UP-8BMW inputs (CN 1/2=Front L/R, 14/15=Rear L/R, 26/27=Subwoofer 1/2, 38/39=Digital In L/R,
54/55=AUX In L/R). `Channel.name` auto-resolves from `CN`. **CN 13 & 25 resolve to `"Unknown
(CN13/25)"`** — they appear in setups authored/migrated from v6.03.03 but are **absent from the
analyzed v6.04.01 map** (the enum was pruned between versions), so we don't assert a name for them
(`CN_NAMES_UNKNOWN` flags the two; the current tool can't validate them, and may itself render a
migrated CN 13/25 as "Not assigned"). So the friendly-names problem is closed: labels come from the
tool's own table, with the two orphaned codes honestly marked Unknown.

## `<Route>` / `<R>` routing blocks — THREE-block structure (RESOLVED 2026-08-28, real M 5.4DSP)
`<Route>` contains one `<R>` block per routing matrix in the DSP signal chain. Each `<R>` is an
`OUTS`×`INS` grid of `G<n>` **linear** mix coefficients (8.24 → float), row-major with
**`n = destination_row · INS + source_col`** (row = the `OUTS`/destination axis, col = the
`INS`/source axis). Confirmed against a real M 5.4DSP file: every nonzero cell decodes in-bounds
and destination←source-consistent under this orientation. The M 5.4DSP emits **three** blocks in
this order:

| # | block | `INS` | `OUTS` | DSP mixer (param template) |
|---|-------|------|-------|-----------------------------|
| R0 | **Main → Virtual** | 6 | 9 | `MOD_MAINMATRIX_…ALG2VOL{row}{col}` (`input_to_virtual`) |
| R1 | **Digital → Virtual** | 2 | 9 | `MOD_OPTOMIXER_2_…ALG5VOL{row}{col}` (`digital_to_virtual`) |
| R2 | **Virtual → Output** | 9 | 9 | `MOD_NXMLINEAR1_…ALG4VOL{row}{col}` (`virtual_to_output`) |

**Main (R0) is 6 inputs wide** — the full `MAINMATRIX`. Its **6 source columns** are (DECODED
DEFINITIVELY vs the DSP PC-Tool oracle, `~/Downloads/input-routing-diag*.pct6`, 2026-08-28):

| MAINMATRIX col | 0 | 1 | 2 | 3 | 4 | 5 |
|---|---|---|---|---|---|---|
| source | Digital L | Digital R | **Main A** | **Main B** | **Main C** | **Main D** |

i.e. the Main physical inputs **A/B/C/D = columns 2/3/4/5** and the **digital pair = columns 0/1**
(NOT Main=0–3 / digital=4–5). The G-number of a cell is **`G = virtual_row · INS(6) + column`** and
the stored value is **`% = linear × 100`** (30 % → `0.30` in 8.24). Oracle checks: routing Input
A/B/C/D → Virtual A at 11/22/33/44 % produced `G2/G3/G4/G5` (row 0, cols 2–5); Input A → Virtual C
@ 30 % produced `G14` (row 2, col 2). The digital L/R ORDER within cols 0/1 is a documented
best-guess (L=0, R=1, matching the R1 `OPTOMIXER` block). NOTE this column map is a SEPARATE axis
from the `<IC> IN` channel-list index above (where Main A–D = IN 0–3, Digital = IN 4–5): the `IN`
attribute numbers the input-channel LIST, while these numbers are the MAINMATRIX source COLUMN.

**Digital (R1) is a SEPARATE 9×2 matrix** — the `OPTOMIXER` (ALG5) — NOT a slice carved out of R0;
named-digital routing belongs here, not in R0's cols 0/1. Earlier `from_device` emitted only R0 +
R2 (the Digital block was entirely missing); it now reads the `OPTOMIXER` as R1. `AOM`/`OFFS` block
metadata is not DSP RAM and is not emitted. The model exposes all three as
`model.routing.{input_to_virtual, digital_to_virtual, virtual_to_output}`; the per-matrix column
map lives in `MatchM54DSP.channels.yaml` `routing.matrices.<name>.sources` (list POSITION = DSP
column), consumed by `channels._input_source_index`.
