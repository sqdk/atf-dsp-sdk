# protocol-evidence.md — the captures/disassembly behind each protocol.yaml entry

Human companion to `../protocol.yaml`. Every non-trivial claim links to the firmware evidence and,
where the finding rested on an assumption, to the **adversarial review** that tried to refute it.

Firmware: `analysis/App_M5.4DSP_Release.bin` (ARM Cortex-M, Thumb, load base 0x4000; file offset =
vaddr − 0x4000). Tools: Ghidra 12.1.3 headless (persistent project + `analysis/DecompDump.java`,
which decompiles a function and resolves indirect-call pointer literals), radare2.

---

## Framing (confirmed)
- RX deframer `FUN_0000481e`: state0 requires 'B' (0x42); reads LEN, ~LEN, TYPE; state2 delivers
  LEN−1 payload bytes + checksum. TX framer `FUN_000048d0`: emits 0x43 / LEN=payload_len.
- Asymmetric: cmd `0x42 | len+1 | ~len | 0x01 | payload | sum`; resp `0x43 | len | ~len | 0x01 | payload | sum`.
- Hardware-confirmed both directions 2026-08-25. (An earlier symmetric `0x43/len` guess for the
  command direction was wrong — caught by re-reading the RX deframer.)

## Dispatch / opcode map (S1, confirmed/firmware)
- `FUN_00004aec` switches on payload[0]. Full case dump (0..0x2b) captured; every indirect handler
  pointer resolved via literal-pool reads (DecompDump). Key handlers:
  - 0x02 read → `FUN_00005da0`; 0x03 write → direct `FUN_00005d28` / safeload `FUN_00005b6c`.
  - 0x08 identify (case 8, builds model/fw/telemetry response).
  - 0x1F config SET → `FUN_00008be8` (switch on sel=byte[3]; case 9 = setup select).
  - 0x1E config GET → `FUN_000090ac` (mirror of 0x1F; default sub-op reads DSP memory).
  - 0x28/0x29 → `FUN_00007b3c`; 0x2a/0x2b → `FUN_00007708`.
- Pre-switch gate `FUN_0000ae30()`: when a controller/remote is active only {7,8,0x13,0x28..0x2b}
  are accepted — so 0x02/0x03/0x1F need the un-gated state.
- Setup switch: `0x1F sel=9 idx=byte[5]` → `FUN_00006998(idx)`; hardware-confirmed no-mute
  (2026-08-25, enable flag stayed 0). The `0x29 40` remote path also switches but MUTES (verified).

## DSP read/write + SafeLoad (S2, confirmed — firmware + 2 adversarial reviews)
Call chain: dispatcher case 2/3 → `FUN_00005da0`/`FUN_00005d28`/`FUN_00005b6c` → SPI workers
`FUN_000053e0` (read) / `FUN_00005344` (write) → SPI TX primitive `FUN_00005330`.

- **SPI shape** = native ADAU145x: read header `01 <addr_hi> <addr_lo>` then N bytes; write header
  `00 <addr_hi> <addr_lo>` then data. Address big-endian (addr_hi first).
- **Word size = 4 bytes (32-bit)**: dispatcher passes byte_count>>2 as word count to SafeLoad;
  `FUN_00005b6c` gates word count ≤ 5 and memcpy's count<<2 bytes. Two inverse shifts ⇒ 4-byte unit.
- **SafeLoad block** (`FUN_00005b6c`, raw Thumb @0x1b6c): 28-byte buffer (32 if instance bit3 set),
  memset 0; up to 5 data words at [0..19]; addr_hi→[22], addr_lo→[23]; word_count→[27] (or [31]);
  written in ONE burst to a window base read from RAM 0x20000c22 (`FUN_00005c5c`, instance 0). This
  is the 7-word ADAU145x SafeLoad register file (5 data + address + NUM; NUM triggers).
- **Flag/instance**: opcode 0x03 byte[1]=instance (honored when instance&7==0), byte[2]=flag
  (0xFF ⇒ direct; else SafeLoad iff payload len < 0x1a), bytes[3:5]=addr BE, bytes[5:]=data BE.

### Hardware read-only confirmation (2026-08-26, live M 5.4DSP on /dev/cu.usbmodem101)
Read-only pass via the `atf_dsp` library (no writes, no side effects):
- **`0x02` DSP read works on hardware.** Returns 4-byte values.
- **8.24 CONFIRMED LIVE:** Output A gain reads back `01000000` = exactly +1.000000 (0 dB unity) —
  the DSP's own default value, not host-written. 5.23 unity would be `00800000`. Independent
  confirmation of the datasheet/AF1 conclusion on the actual chip.
- **Word size = 4 bytes, big-endian CONFIRMED:** reading 4 BE bytes yields sane 8.24 values
  (unity gain; ±2 biquad coeffs); wrong size/endianness would be garbage.
- Biquad coefficients read cleanly (output `EQUALIZER` and virtual `GLOBAL_EQ`); setup=10; telemetry
  returns data. So S2's **read path + 8.24 + word size/endianness are hardware-confirmed**.
### Hardware WRITE confirmation (2026-08-26, live)
- **`0x03` write path CONFIRMED on hardware — both direct (flag=0xFF) and firmware SafeLoad
  (flag!=0xFF).** snapshot→write→readback→restore: writing 0x00804DCE (−6 dB) to Output A gain reads
  back 0x00804DCE then restores to 0x01000000; a 5-word EQ burst reads back exactly the computed 8.24
  biquad. Works with or without an identify() session.
- **Bug found + fixed via hardware:** the library's `Controls` was building the SafeLoad block
  *host-side* and direct-writing it to a hardcoded window base `0x0014` (ADAU1452 Table-58 default),
  which is NOT this unit's compiler-assigned base, so those writes silently no-op'd. The firmware's
  own `0x03` safeload path (FUN_00005b6c) reads the true base at runtime (RAM 0x20000c22) and works.
  Fix: `Controls._write_words`/`set_eq_band`/`set_crossover` now send opcode `0x03` (safeload selector)
  and let the firmware perform the SafeLoad — never build it host-side. (The first hw run's "writes
  no-op" was this bug, not the hardware.)
- Caveat unchanged: readback confirms storage / round-trip / addressing; *audible* correctness (dB
  level, fs 48 vs 96 kHz) still needs RTA measurement, not readback.
- Minor: `identify()` parses the wrong model field (`66666666`); the raw carries `M54DSP` + fw
  `2026.03.11-12:16` — parser fix pending.

### Adversarial reviews (S2)
Two independent subagents re-derived the claims from the bytes, each prompted to REFUTE.
- **Review A (word size + byte order):** CONFIRMED 32-bit words (three inverse-shift proofs:
  dispatcher `lsrs r6,r6,2` ↔ safeload `lsls r2,r4,2` ↔ memcpy count*4, gate `cmp r4,5`).
  CONFIRMED big-endian *address* (enforced `>>8` before `&0xff` at 0x135a/0x1362, 0x13ea/0x13ee,
  0x1bb0/0x1bba, and wire parse 0x4be4). Refinement: the firmware does **not** byte-swap or validate
  32-bit *data* words — it is verbatim, order-preserving passthrough. ⇒ the host MUST supply
  big-endian data (correct for the big-endian ADAU145x); recorded as "confirmed-by-passthrough".
- **Review B (SafeLoad + gating):** CONFIRMED the block layout and byte offsets [22]/[23]/[27],
  the ≤5-word gate, the single-burst write, and the window base at 0x20000c22. **Refuted one
  sub-claim**: the instance gate is `(instance & 7)==0`, not `instance==0` — 0/8/16/… are honored,
  and instance=8 selects the 32-byte block (count at [31]). Also noted: a 2-byte SafeLoad-enable
  register write (reg 0xf899) may precede the burst; opcode 0x03 is blocked entirely when the
  secure/controller-active state is set.
- **Net:** both corrections folded into `protocol.yaml` (instance mask 0x07; data-endianness note).
  Status is firmware+review — a hardware read→write→read-back round-trip is still pending to reach
  hardware-confirmed.

## Value encoders (S3, fixed-point confirmed)
- **Fixed-point format = 8.24 — TRIPLE-confirmed** (this was the one point web sources contradicted):
  1. ADI ADAU1452/1451/1450 datasheet Rev C p.76-77: "8.24 data format … same numeric format for
     parameter and data values", unity = 0x01000000, 0.5 = 0x00800000, 2.0 = 0x02000000, signed
     two's-complement, clamp ±128.
  2. **Empirical AF1 check** (`scratchpad/validate_824.py` over `MatchM54DSP_1.AF1`): **792 word-aligned
     `0x01000000` (8.24 unity)** words (unity gains + bypassed-biquad B0), vs only ~39 aligned
     `0x00800000` (which is 0.5 in 8.24, not unity). If the format were the ADAU170x 5.23, unity would
     be 0x00800000 and dominate — it does not. Decisive.
  3. Byte order is literally `01 00 00 00` = big-endian, matching the S2 SPI finding.
  The ADAU170x/MCUdude `2**23` (5.23) does NOT apply to this chip — recorded as the counter-example.
- `.at01` orders EQ biquad coefficients on consecutive addresses as STAGE{n}_B2, B1, B0, A2, A1
  (confirmed by inspection). Encoders: gain = round(10^(dB/20)·2^24); mute = 0 / 0x01000000; delay =
  integer sample count (32.0 format); biquad = RBJ normalized by a0, storing −a1/a0, −a2/a0 (a1,a2
  inverted per ADI SigmaStudio wiki). Worked +6 dB/1 kHz/Q1/48 k example recorded in protocol.yaml.
- **SafeLoad addresses corroborated**: datasheet Table 58 defaults data 0x0014-0x0018, address 0x0019,
  num/trigger 0x001A (5+1+1 = 7 words) — exactly the RE'd block; the runtime window base (RAM
  0x20000c22) is the in-use base.
- Remaining needs-hardware: internal fs (48 vs 96 kHz, changes all biquad coeffs), exact delay-cell
  semantics, and MUTE hard-vs-slew behavior. These are flagged in protocol.yaml.
- The `.AF1` default image is an ASCII C-array of bytes (SigmaStudio download); the 32-bit param
  words are 4-byte-aligned big-endian (phase-0 alignment carries the 792 unity words).

## RTA / Input Signal Analyzer (S4, partial)
- Mechanism = client-polled readback cells (not a device push-stream). `MOD_INPUTRTA` (19 params) +
  peak/RMS readback cells (input RTA 4957, input peak 4944, output peak 8451, vcp peak 6813, volume
  4600, sigdet 4958), a channel-select mux (8541) and peak-reset (8509). Read via 0x02 (or the 0x1E
  structured-read default sub-op). Exact cell set + cadence need a PC-Tool capture.

## Multi-model (S5, confirmed)
- HELIX DSP.3 (`App_DSP3_Release.bin`): SafeLoad handler byte-identical to M5.4DSP, relocated
  0x5b6c→0x5d98 (same `movs r2,0x20`, `movs r6,0x1c`, `cmp r4,5/bhi`, `strb [r2,0x16]`/`[r3,0x17]`).
  Shared codebase across all 35 models; per-model deltas = `.at01` map, PID, literal-pool RAM addrs.
