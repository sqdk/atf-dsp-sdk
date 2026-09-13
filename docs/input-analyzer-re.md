# Input Signal Analyzer (ISA) — RE-0 findings

Reverse-engineering of the **DSP PC-Tool 6**'s live **Input Signal Analyzer** (the electrical,
on-chip input-signal spectrum shown in the UI). Source binary:
`extracted/app/ATF_DSP_PC-TOOL_6.exe` (32-bit PE/x86, MSVC, Qt5 + `qwt.dll` plot widget).
Corroborated against the M 5.4DSP firmware (`analysis/App_M5.4DSP_Release.bin`) and the
`MatchM54DSP.at01` param map. **Static only — not yet validated on hardware.**

TL;DR: the Input Signal Analyzer is **not** an on-chip FFT / fixed bin buffer. It is a
**host-driven, stepped single-bandpass sweep**: the PC-Tool retunes ONE tunable analysis biquad on
the DSP to each frequency point, waits an (adaptive) settle time, and reads ONE RMS readback cell.
The "spectrum" and its frequency axis are constructed **on the host**; the DSP exposes a
single-value level probe plus a tunable filter.

---

## 1. The feature and the class that drives it

The UI feature is the **"(Advanced) Input Signal Analyzer"** ("ISA RTA MODE"). It is implemented by
a C++ class **`InputRTA`** (RTTI `.?AVInputRTA@@` @ .data 0x02946ab4). Distinct from the
microphone-based acoustic RTA (`AudioAnalyzer` / `HDAnalyzer` / `AnalyzerWindow` / `dcmRTAConfig`,
the "Real Time Analyzer" / TuneToTarget pink-noise flow) — that one uses a measurement mic and is
**not** RE-0. The ISA is fully electrical/on-chip.

Diagnostic strings (exe): `"Advanced Input Signal Analyzer"`, `"Input Signal Analyzer"`,
`"ISA RTA MODE"`, `"InputRTA"`, `"InputRTA requires at least one input channel"`,
`"Real Time Analyzer - Selected Channel(s): %1"` (note **plural** — the ISA can sum several inputs),
and the three address-check errors `"No Address for rtaFilter1 in IRA"`,
`"No Address for rtaHpfilter in IRA"`, `"No Address for rtaVolumeDet1 in IRA"` (IRA = Input RTA).

### `InputRTA` object layout (from `checkAddresses()` @ 0x0061aee0 and dtor @ 0x0061ada0)
| member | role | bound DSP param (M 5.4DSP `.at01`) |
|---|---|---|
| `this+0x4c` `rtaFilter1`   | tunable analysis biquad (retuned per point) | `MOD_INPUTRTA_INPUTRTAFILTER1` targets `TARGB210..214` @ **39450–39454** (+ `LAMBDA_1` slew @ 4950) |
| `this+0x50` `rtaHpfilter`  | fixed pre-filter (HP / DC-block) | `MOD_INPUTRTA_GENFILTER6_ALG0_STAGE0_{B2,B1,B0,A2,A1}` @ **4951–4955** |
| `this+0x5c` `rtaVolumeDet1`| RMS envelope readback (the level probe) | `MOD_INPUTRTA_RBINPUTRTA1_READBACKALGNEWSIGMA3004VALUE` @ **4957** |
| `this+0x54/0x58` | input source mixer + bookkeeping | `MOD_INPUTRTA_NXMINPUTRTA_ALG0_...VOL0000..0003` @ **4940–4943** |

Member↔param binding is read straight from the PC-Tool's DCM address-registration table
(function @ 0x0064c480, which pushes the `DSP1_MOD_INPUTRTA_*_ADDR` name strings for exactly these
cells: NXMINPUTRTA VOL0000, INPUTRTAFILTER1 TARGB210, GENFILTER1 B2, RBINPUTRTA1 VALUE).

The on-chip signal chain is therefore:
```
selected input(s) → NXMINPUTRTA mixer (4940-4943) → GENFILTER6 HP (4951-4955)
                   → INPUTRTAFILTER1 tunable BPF (targets 39450-39454) → MSENV2 RMS envelope
                   → RBINPUTRTA1 readback (4957)   ← host reads this
```
(`MOD_INPUTRTA_MSENV2` @ 8543/8544/8545 = the RMS/HOLD/DECAY envelope detector feeding RBINPUTRTA1.)

---

## 2. How it reads the data (opcode / addresses / format)

**Opcodes:** the confirmed generic DSP primitives — **read = `0x02`**, **write (retune) = `0x03`
SafeLoad**. No dedicated analyzer/telemetry opcode; the ISA is built entirely on plain DSP
memory read/write of the cells above. (The `0x1E` structured-read path could also reach 4957, but
the PC-Tool uses the object's own read/write, i.e. `0x02`/`0x03`.)

**Per-point sweep step** — decompiled from `InputRTA` @ ~0x0061b2f0–0x0061b3d6:
1. Compute the analysis-filter parameters for the point's centre frequency (a per-point value pulled
   from a host-side `QVector`, e.g. `[ebx+0x14]` / `[ebx+0x18]`). Constants used: `20000.0`
   (`0x40d3880000000000`), `100.0` (`0x4059000000000000`), `2.0` clamp, `1.0`.
2. `mov ecx,[ebx+0x4c]` (rtaFilter1) → set two coeff params (`call 0x5e5c60`, `call 0x5e5e50`) →
   `call 0x5e3a60(0)` → `call [vtable+0x34]` (**commit** → writes the filter's 5 target coefficients
   `TARGB210..214` at 39450–39454 to the DSP via `0x03` SafeLoad; 5 words fit one burst).
3. Arm `QTimer::singleShot(delay, …)` with the callback @ 0x0061b0e0; **the delay is adaptive**,
   computed per point ≈ `max(2.0, (20000/centre/100)·k)` ms — i.e. a floor of ~2 ms, growing toward
   low frequencies (longer settle where the RMS envelope needs more cycles).
4. Callback 0x0061b0e0: `mov ecx,[ecx+0x5c]` (rtaVolumeDet1) → read RBINPUTRTA1 via `0x02`, decode,
   store into the plot buffer. (rtaVolumeDet1 also owns its own `QTimer` start/stop — setter
   @ 0x5dcd20 — so the readback is genuinely client-polled, not pushed.)
5. Advance to the next frequency point and repeat (driver loop @ 0x0061b120 / 0x0061b6a0 iterating a
   point vector `[ebx+0x40]` index vs `[ebx+0x18]` count).

**Numeric format:** the RBINPUTRTA1 value is a **32-bit big-endian 8.24 fixed-point** word (the
project-wide confirmed format), read as 4 bytes and decoded with `from_fixed` → a linear magnitude;
`dBFS = 20·log10(|value|)` (1.0 == full scale). The filter target coefficients written per point are
also 8.24, matching the confirmed `eq_band` biquad encoder.

**Channel selection:** the ISA selects/sums input taps by writing the **NXMINPUTRTA mixer** cells
`VOL0000..0003` @ 4940–4943 (unity to include a tap, 0 to exclude) — this is why the UI says
"Selected Channel(s)" (plural) and "requires at least one input channel". This is a **different**
control from the input **level bar** meter (`MOD_INPUT_LEVEL_*`: MonoMux `CHANNELSELECT` @ 8541,
`PEAKREADBACK` @ 4944, `RESETHOLD` @ 8509), which is a single-channel broadband peak readout.
See the correction in §4.

**Bin / frequency mapping:** there is **no fixed on-chip bin table**. The set of frequency points
(count + spacing) is chosen on the **host** (UI: `setRTAPoints`/`emitSetRTAPoints`, "Frequency
Range", "High/Low Frequency Config", `FrequencyValidator`). Each host point maps to one bandpass
retune + one RMS read. So "frequency of bin *i*" = the centre frequency the host wrote for point *i*;
it is exact and host-defined, not a quantity to recover from the DSP.

**Cadence:** per-point, adaptive settle (≥ ~2 ms, longer at low frequency), driven by
`QTimer::singleShot`; the readback object polls on its own `QTimer`. One full spectrum = Σ over the
host's points. There is no single fixed frame rate.

---

## 3. Adversarial refutation notes

Re-derived from independent code paths and actively tried to break the conclusion:

- **"Is it really swept, or a fixed filter + broadband level meter?"** The analysis filter is an
  `EQS300MULTIDPSWSLEW` (multi-datapoint **software-slew** biquad) exposing writable **target**
  coefficients `TARGB210..214`, and the sweep step provably **commits new filter coefficients before
  each read** (0x61b34d–0x61b38d: three coeff setters + a vtable commit on `[+0x4c]`). A fixed filter
  would never touch those targets at runtime. ⇒ swept, confirmed from the code, not assumed.
- **"Is there a hidden multi-bin buffer?"** The `.at01` has exactly **one** `RBINPUTRTA1` readback
  cell for the ISA (4957) and **one** `INPUTRTAFILTER1`. A per-bin buffer would need N readbacks.
  ⇒ single-bin probe, confirmed.
- **"Wrong cell?"** The exe's own registration table binds the `InputRTA` members to precisely
  `INPUTRTAFILTER1 TARGB210`, `GENFILTER1 B2`, `RBINPUTRTA1 VALUE`, `NXMINPUTRTA VOL0000` (function
  @ 0x0064c480). Re-derived independently from `checkAddresses()` (@0x61aee0) which references the
  three "No Address for rta{Filter1,Hpfilter,VolumeDet1}" errors against members +0x4c/+0x50/+0x5c.
  Two paths, same three blocks. ⇒ addresses confirmed for M 5.4DSP.
- **Byte order / fixed-point:** RBINPUTRTA1 is decoded with the project-confirmed 8.24 big-endian
  (`0x01000000`=1.0), the same format hardware-verified for gains/EQ on 2026-08-26. Not re-litigated.

### Hardware validation — 2026-08-29 (M 5.4DSP bench)
Closed on the amp (tone injected into the 4-ch inputs, outputs muted; see
[[bench-session-2026-08-29]]):
- ✅ **Read path** — RBINPUTRTA1 responds via `0x02`, sane 8.24; level −108 dBFS floor → −58 with tone.
- ✅ **Mechanism + frequency map** — swept single-bandpass; a 1 kHz tone peaked at commanded 1000 Hz,
  a 3150 Hz tone at commanded 3200 Hz (~1.6 %), and the peak **tracked** across 1 k→4 k→3.15 k. This
  implicitly confirms the **TARGB storage order** and the analysis-filter kind/Q (a wrong order would
  not centre correctly) — items 1 & 2 below are effectively resolved for practical use.
- ✅ **Tap select** — `select_input` (NXMINPUTRTA VOL) demonstrably switches taps; taps 0/1 = the
  front pair carried the tone (rear 2/3 lower).

### What remains APPROXIMATE (non-blocking)
3. **NXMINPUTRTA tap→input-LETTER mapping** — only front/rear *grouping* is confirmed (0/1 = front);
   the exact per-letter map still needs a one-input-at-a-time capture.
4. **Absolute calibration** (raw 8.24 RMS → dBFS/dBu) — NOT NEEDED per the bench decision: input-EQ
   flatten is shape-based, so relative dBFS + a fixed offset suffices. We report relative dBFS.

**Confidence:** the *mechanism* + *frequency map* are now HARDWARE-CONFIRMED; only the exact tap→letter
map and absolute dB level remain approximate (and neither blocks input-EQ flatten). `analyzer.py`
promoted out of PROVISIONAL 2026-08-29.

---

## 4. Correction to the existing `rta` contract

`protocol.yaml.rta` (status: partial) and `acodsp/rta.py` currently treat `channel_select = 8541`
(`MOD_INPUT_LEVEL_CHANNELSELECT`, a MonoMux) as the analyzer channel select, and list a flat set of
readback cells. That conflates **two different features**:

- **Input Signal Analyzer (ISA / `InputRTA`)** — swept spectrum: select/sum via **NXMINPUTRTA**
  (4940-4943), retune **INPUTRTAFILTER1** (39450-39454), read **RBINPUTRTA1** (4957).
- **Input level meter** — single-channel broadband peak bar: **INPUT_LEVEL** MonoMux
  `CHANNELSELECT` (8541) + `PEAKREADBACK` (4944) + `RESETHOLD` (8509).

`protocol.yaml` is intentionally **left unchanged** for now (mechanism is static-only, not hardware
-validated). This doc is the record; fold into `protocol.yaml.rta` once a live capture/tone test
confirms it. New PROVISIONAL code lives in `atf_dsp_control/acodsp/analyzer.py` (does not touch the
existing `rta.py`).

## 5. Evidence index (addresses in the exe unless noted)
- RTTI `.?AVInputRTA@@` @ 0x02946ab4; strings "ISA RTA MODE" @0x028895d0, ISA errors @0x0288abf0/
  0x0288ac14/0x0288ac38.
- `InputRTA::checkAddresses` @ 0x0061aee0; dtor @ 0x0061ada0 (member offsets +0x4c/0x50/0x54/0x58/0x5c).
- DCM address-registration (member↔param binding) @ 0x0064c480.
- Sweep step (retune+commit+singleShot) @ ~0x0061b2f0–0x0061b3d6; read callback @ 0x0061b0e0;
  point-loop @ 0x0061b120 / 0x0061b6a0; readback-setter+timer @ 0x005dcd20.
- Firmware: DSP read `0x02` → FUN_00005da0/FUN_000053e0; write `0x03` SafeLoad → FUN_00005b6c
  (see `docs/protocol-evidence.md` §S2). `.at01` addresses from `analysis/MatchM54DSP.at01.inflated.h`.

---

## Adversarial review (independent)

_Independent RE-1 pass, 2026-08-27. Re-derived cold from `extracted/app/ATF_DSP_PC-TOOL_6.exe`
(baddr 0x400000; .text 0x401000–0x79c000) and `analysis/MatchM54DSP.at01.inflated.h` — not from the
claim text. radare2 disassembly + param-map/string xref. Goal was to break the model, not confirm it._

### Claim 1 — feature = class `InputRTA`, distinct from the mic RTA — **CONFIRMED**
- RTTI `.?AVInputRTA@@` @ vaddr 0x02946ab4 (paddr 0x025450b4) — matches.
- Six distinct analyzer RTTI classes coexist: `.?AVInputRTA@@` (ISA, electrical) vs the mic path
  `.?AVAudioAnalyzer@@`, `.?AVHDAnalyzer@@`, `.?AVAnalyzerWindow@@`, `.?AVdcmRTAConfig@@`, plus qwt
  helper `.?AVLogScaleDrawRTA@@`. The mic path is unambiguously the TuneToTarget pink-noise flow
  ("Play back correlated pink noise… let the Analyzer run", "Measurement Microphone"). No mix-up:
  InputRTA has its own error strings ("… in IRA") and its own DSP cells. Separation is real.

### Claim 2 — host-driven stepped single-bandpass sweep, NOT on-chip FFT/multi-bin — **CONFIRMED**
Actively hunted for a multi-bin/FFT readback and it is **absent**:
- The whole `.at01` map contains **one** `RBINPUTRTA1` readback (4957) and **one** `INPUTRTAFILTER1`
  (LAMBDA_1 @4950 + 5 TARGB @39450–39454). `grep -c` = 1 and 6 respectively.
- **Zero** cells match `FFT|SPECTRUM|_BIN|MAGNITUD`. There is no bin table on-chip.
- 4957 is NOT part of a contiguous readback block: its neighbours are `INPUT_LEVEL PEAKREADBACK`
  @4944, `SIGDET` @4958, `MAINMATRIX VOL` @4959 — all unrelated modules. Every readback cell in the
  map (4600 volume, 4601 clipping, 4944 input-peak, 6813 vcp-peak, 8451 output-peak, 4957 RTA, 4958
  sigdet) is a distinct single-value probe for a different feature. A multi-bin buffer would need N
  contiguous `…VALUE` cells; they do not exist.
- The exe's DCM registration (0x64c480, below) registers exactly ONE filter + ONE readback for
  InputRTA. Two independent sources (param map + exe) agree: single probe + single tunable filter.
- The sweep step (0x61b34d–0x61b38d) provably **commits new filter coefficients before each read**.
  A fixed-filter + broadband-meter model is refuted by that write. ⇒ swept, from code.

### Claim 3 — per-point mechanism (addresses/opcodes/format) — **CONFIRMED (mechanism); quantitative filter math UNPROVABLE-WITHOUT-HARDWARE**
All addresses verified against the M 5.4DSP `.at01`:
- NXMINPUTRTA `VOL0000..0003` @ **4940–4943** ✓; INPUTRTAFILTER1 `TARGB210..214` @ **39450–39454**
  (+ `LAMBDA_1` @4950) ✓; RBINPUTRTA1 `…VALUE` @ **4957** ✓.
- Sweep step @ 0x61b2f0–0x61b3d6 (exact range as cited): `mov ecx,[ebx+0x4c]`(rtaFilter1) →
  `call 0x5e5c60` → `call 0x5e5e50` (two coeff setters, fed a per-point double from the QVector at
  `[ebx+0x14]`/`[ebx+0x10]` indexed by point index `[ebx+0x40]`) → `push 0; call 0x5e3a60` →
  `mov eax,[ecx]; call [eax+0x34]` (**vtable commit** = the SafeLoad `0x03` write of the 5 targets).
- Adaptive settle: `cvttsd2si edi, xmm1` (delay ms→int) → alloc 0xc-byte functor → store invoke fn
  `0x486210` + **callback 0x61b0e0** → `QTimer::singleShot(edi, functor)`. The delay math uses
  FP constants I decoded from the binary: **2.0** (@0x284fb00, the `maxsd` floor), **20000.0**
  (@0x28680a0), **100.0** (@0x28542f0), **1.0** (@0x284faf8) — i.e. `delay = max(2.0,
  (20000/centre/100)·k)` ms, floor byte-exact. Inverse-frequency shape confirmed.
- Read callback @ 0x61b0e0: `mov ecx,[ecx+0x5c]`(rtaVolumeDet1) → tail-jmp 0x576ff0. Read is on the
  `+0x5c` readback object; the `0x02` opcode itself rests on the project-wide S2 read primitive (the
  callback tail-jumps into a getter/poll path, not an inline `push 2`, so the opcode is inherited,
  not re-proven at this site — see live-check list).
- **8.24 big-endian** decode and `20·log10` are the project-confirmed format (S3); not re-litigated.
- UNPROVABLE here: the analysis-filter kind/Q and that `TARGB210→B2 … TARGB214→A1` for the
  EQS300MULTIDPSWSLEW (software-slew) filter. The 5-word count/base/`0x03` route ARE confirmed.

### Claim 4 — signal chain — **CONFIRMED at the endpoints; internal wiring PLAUSIBLE**
Every named cell exists at the stated address: NXMINPUTRTA 4940–4943 → GENFILTER6 HP
`B2/B1/B0/A2/A1` @ **4951–4955** → INPUTRTAFILTER1 BPF (39450–39454) → MSENV2
`RMS/HOLD/DECAY` @ **8543/8544/8545** → RBINPUTRTA1 @ **4957**. `checkAddresses` binds these to object
members +0x4c (filter1), +0x50 (hpfilter), +0x58 (inputMixer), +0x5c (volumeDet1). The exact
intra-DSP routing order (that MSENV2 sits between the BPF and RBINPUTRTA1) is inferred from module
naming, not traced in firmware — PLAUSIBLE, and consistent.

### Claim 5 — ISA vs the broadband INPUT_LEVEL bar was conflated — **CONFIRMED, split is correct**
The `MOD_INPUT_LEVEL_*` module is separate and complete in the map: `PEAKREADBACK` @ **4944**,
`CHANNELSELECT` MonoMux (NS24) @ **8541**, `RESETHOLD` @ **8509** — a single-channel broadband peak
bar, a different module from `MOD_INPUTRTA_*`. Treating 8541 as the analyzer's channel select
(old `rta.py`) was wrong; the analyzer selects via NXMINPUTRTA 4940–4943. Correction stands.

### Claim 6 — cited anchor addresses do what's claimed — **CONFIRMED**
- `checkAddresses` @ **0x61aee0**: validates `[edi+0x4c]`→"No Address for rtaFilter1 in IRA",
  `[edi+0x50]`→"…rtaHpfilter…", `[edi+0x5c]`→"…rtaVolumeDet1…", **and a 4th** `[edi+0x58]`→"No
  Address for inputMixer in IRA". (The claim listed +0x58 as "bookkeeping"; it is explicitly the
  inputMixer — minor correction.) Two validator helpers: 0x5e4d90 for the filter members, 0x5dcc80
  for the readback/mixer members.
- DCM registration @ **0x64c480**: pushes exactly four InputRTA name strings, in order — 
  `DSP1_MOD_INPUTRTA_NXMINPUTRTA…VOL0000_ADDR`, `…INPUTRTAFILTER1…TARGB210_ADDR`,
  `…GENFILTER1_ALG0_STAGE0_B2_ADDR`, `…RBINPUTRTA1…VALUE_ADDR`. **Note:** the exe's registration
  template names the HP prefilter `GENFILTER1`, whereas the M 5.4DSP `.at01` resolves that slot to
  `GENFILTER6` @4951 (the exe also carries a `MOD_INPUTRTA_GENFILTER6…B2` name string) — a per-model
  naming variant, same slot. One filter, one readback registered ⇒ reinforces Claim 2.
- Sweep step @ **0x61b2f0–0x61b3d6** and read cb @ **0x61b0e0**: as detailed under Claim 3.

### analyzer.py sanity check (report only — not modified)
No address/opcode/format/endianness mismatch, and no unsafe-gating gap:
- Param names resolve to the correct cells (RBINPUTRTA1→4957, TARGB210 base→39450, VOL000{0..3}→
  4940–4943, INPUT_LEVEL peak/select/reset→4944/8541/8509). ✓
- Reads use `read_param(nbytes=4)` + `int.from_bytes(…, "big")` + `from_fixed` (8.24 BE), dBFS =
  `20·log10`. ✓ Writes use `to_bytes_be` + `write_param(safeload=True)` → firmware `0x03`. ✓
- `tune()` writes the 5 TARGB words as ONE contiguous SafeLoad burst from base 39450 → 39450–39454. ✓
- `bandpass_coeffs` stores `[b2/a0,b1/a0,b0/a0,-a2/a0,-a1/a0]` in `[B2,B1,B0,A2,A1]` — **identical**
  to the hardware-confirmed `encoding.biquad_rbj` / `EQ_STORAGE_ORDER`. Internally consistent.
- Gating correct: `select_input`/`tune`/`sweep` require `unsafe=True`; `read_level`/
  `read_level_linear`/`read_input_peak` and the default `read_input()` path are read-only. ✓
- Fidelity notes (not bugs, all gated + documented): `settle_ms` uses `max(2.0, 6000/f0)`; the exe's
  actual factor is `(20000/centre/100)·k` — the 2.0 floor matches, the scale factor `k` does not
  (exe `k` not recovered). `DEFAULT_Q`/`DEFAULT_POINTS`/log-sweep are host guesses, not the tool's
  QVector. TARGB→[B2..A1] mapping is assumed. None affect read-only safety.

### Overall verdict — **GO** to build the MCP PoC on the host-driven stepped-bandpass-sweep model
The mechanism is confirmed from three independent sources that agree: the `.at01` param map, the exe
DCM registration (0x64c480), and the decompiled object (`checkAddresses` 0x61aee0 + sweep step
0x61b2f0 + read cb 0x61b0e0). No FFT/multi-bin readback exists. The read-only probes in analyzer.py
are safe to ship now; the sweep/write path is correctly gated behind `unsafe=True`. Nothing refuted;
two minor corrections (the 4th `inputMixer` address check; the GENFILTER1↔GENFILTER6 per-model name).

### Still needs live-hardware confirmation (build with these gated)
- That the RBINPUTRTA1 read is genuinely opcode `0x02` on 4957 in a live capture (inherited from S2,
  not re-proven at the callback site).
- `TARGB210..214` → `[B2,B1,B0,A2,A1]` order for the EQS300MULTIDPSWSLEW filter, and its exact kind/Q.
- The exact settle-time factor `k` (only floor 2.0 / 20000 / 100 / 1.0 recovered) and the tool's
  default frequency-point QVector (count + spacing).
- NXMINPUTRTA tap→input-letter mapping (4 VOL cells @4940–4943 vs 6 physical inputs).
- Absolute calibration: raw 8.24 RMS → dBFS/dBu offset (needs a known-tone injection).
- Tone test: inject 1 kHz into a selected input, confirm the 1 kHz point rises and neighbours don't;
  sweep the tone and confirm the peak tracks. Only then promote from PROVISIONAL.
</content>
</invoke>
