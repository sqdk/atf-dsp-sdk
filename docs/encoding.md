# Value encoding — per control type

Source of truth: `../protocol.yaml` (`encoders:`, `dsp_rw.safeload`, `rta:`). Every
format below is implemented as a pure function in `atf_dsp/encoding.py` (no I/O) and
exposed as a typed method in `atf_dsp/controls.py`. The fixed-point parameters are read
at import from the bundled `atf_dsp/data/protocol.yaml` (`encoders._fixed_point`) rather
than hard-coded, so a contract revision flows through without a code change.

All DSP words are **32-bit, big-endian** on the wire (`dsp_rw.byte_order: big`); the
firmware does not byte-swap.

## Fixed-point: 8.24 (`encoders._fixed_point`, status confirmed)
- 8 integer + 24 fractional bits, signed two's-complement, 32-bit.
- `unity = 0x01000000` (+1.0), `minus_one = 0xFF000000`, clamp `[-128.0, 127.99999994]`.
- `to_fixed(v) = clamp(round(v * 2**24))`, saturated to int32; `from_fixed` is the inverse.
- Sentinels asserted in `tests/test_encoding.py`: `0.5 -> 0x00800000`, `2.0 -> 0x02000000`,
  `1.0 -> 0x01000000`, `-1.0 -> 0xFF000000`.
- Evidence (protocol.yaml): ADI ADAU1452 datasheet Rev C p.76-77 (8.24, unity 0x01000000,
  0.5=0x00800000, 2.0=0x02000000); AF1 default image `MatchM54DSP_1.AF1` holds 792 aligned
  `0x01000000` words (unity gains + bypassed biquad B0) vs ~0 aligned 5.23-unity — so the
  chip is 8.24, **not** the ADAU170x 5.23. Byte order literally `01 00 00 00` = big-endian.

## Gain / volume (`encoders.gain`, status confirmed)
- `raw = to_fixed(10**(dB/20))` — an 8.24 linear multiplier.
- Sentinels: `0dB->0x01000000`, `-6dB->0x00804DCE`, `+6dB->0x01FEC983`, `-60dB->0x00004189`.
- Param addresses: `*GAIN*` / `MOD_GAIN_*` / `MOD_INPUTGAIN_*` / `MOD_VOLUME_*` / `MOD_HECGAIN_*`.
- Method: `Controls.set_gain(name_or_addr, dB)`.

## Mute (`encoders.mute`, status confirmed; on/off detail needs hardware)
- Dedicated 8.24 gain-multiplier cell, separate from the level: `unmute = 0x01000000`,
  `mute = 0x00000000`. Addresses: `*MUTE*` (e.g. `MOD_OUTPUTMUTE`, `MOD_VCPMUTE`).
- Method: `Controls.set_mute(name_or_addr, muted: bool)`.

## Delay / time alignment (`encoders.delay`, status **hypothesis** — gated)
- `samples = round(ms/1000 * fs)`, stored as a 32-bit integer (32.0 logic format, not 8.24).
- Sentinels @48k: `10ms->480 (0x000001E0)`, `100ms->4800 (0x000012C0)`.
- Addresses: `MOD_DELAY_*`. Internal fs (48 vs 96 kHz) and exact cell semantics need a
  hardware capture, so `Controls.set_delay(...)` raises `NotConfirmedError` unless `unsafe=True`.

## EQ band — RBJ biquad (`encoders.eq_band`, status confirmed; internal fs needs HW)
- RBJ Audio-EQ cookbook (reimplemented from first principles — public domain — so no
  LGPL obligation from MCUdude/SigmaDSP), normalised by `a0`, stored with the SigmaStudio
  sign inversion (`-a1/a0`, `-a2/a0`).
- On-chip storage order **[B2, B1, B0, A2, A1]** = `[b2/a0, b1/a0, b0/a0, -a2/a0, -a1/a0]`,
  written to 5 consecutive `STAGE` addresses in one SafeLoad burst (glitch-free).
- Kinds: `peaking, lowshelf, highshelf, lowpass, highpass`. A 0 dB peaking band is stored
  as a true bypass `[0, 0, 0x01000000, 0, 0]` (matches the AF1 default image).
- Sentinel — `+6dB peaking, f0=1kHz, Q=1.0, fs=48k`:
  `[B2,B1,B0,A2,A1] = [0x00DE230C, 0xFE1ACC43, 0x010B4082, 0xFF169C71, 0x01E533BD]`.
- Evidence: `at01` STAGE{n}_B2,B1,B0,A2,A1 consecutive-address order confirmed by inspection;
  a1/a2 inversion per ADI SigmaStudio wiki; 5-coeff biquad per datasheet SafeLoad (p.80).
- Method: `Controls.set_eq_band(base_name_or_addr, f0, Q, gain_db, kind)`.

## Crossover / routing (`encoders.crossover`, `encoders.routing`, status **hypothesis** — gated)
- Crossover = the same biquad primitive cascaded per slope; routing = 8.24 MainMatrix mix
  gains. Per-slope stage mapping and matrix cell addressing need a capture, so
  `Controls.set_crossover(...)` / `set_routing(...)` require `unsafe=True`.

## SafeLoad block (`dsp_rw.safeload`, status confirmed)
- 28-byte block = 7 consecutive 32-bit big-endian words, written in ONE burst via the 0x03
  **direct** path (flag 0xFF) to the SafeLoad window base:
  - `words[0..4]` up to 5 data words (zero-padded), bytes `[0..19]`;
  - `word[5]` target DSP address, `addr_hi` at byte `[22]`, `addr_lo` at byte `[23]`;
  - `word[6]` NUM = word count at byte `[27]` — writing it triggers the atomic load.
- Window base defaults to the ADAU1452 Table-58 data slot `0x0014` (data `0x0014..0x0018`,
  address `0x0019`, NUM `0x001A`); the firmware's true base is read at runtime from RAM
  `0x20000c22`, so `build_safeload_block` / `safeload_write` take a `window_base` override.
- Evidence: FUN_00005b6c (28-byte block, `<=5` words, addr at 22/23, NUM at 27); two
  adversarial firmware reviews. Byte offsets in `tests/test_safeload.py`.

## RTA / Input Signal Analyzer (`rta`, status partial)
- Client-polled: write a channel index to the channel-select mux (`8541`), optionally reset
  peak-hold (`8509`), then read 8.24 readback cells via 0x02 and convert with
  `linear_to_dbfs` (`20*log10|value|`, floored). Cells: `input_rta 4957`, `input_peak 4944`,
  `output_peak 8451`, `vcp_peak 6813`, `volume_rb 4600`, `sigdet 4958`.
- The read path is the confirmed 0x02 primitive (side-effect-free); the exact polling
  set/cadence still needs a PC-Tool capture. API: `atf_dsp/rta.py` `RTA`.
