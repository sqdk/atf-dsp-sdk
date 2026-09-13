# Conductor protocol — Audiotec Fischer ACO remote/display accessory

_RE mission 2026-08-28. No hardware this session; every hardware-dependent claim is **PROVISIONAL**.
Builds on `protocol.yaml` and `docs/protocol-evidence.md`._

The **Conductor** (called **URC** = "Universal Remote Control" in the firmware/PC-Tool) is ATF's wired
remote/display accessory for the ACO DSPs: a rotary volume knob + setup-switch buttons + status display.

There are **THREE distinct planes** that all bear the "Conductor" name — keep them separate:

| Plane | Transport | Who talks | What it is |
|---|---|---|---|
| **A. SCP** (PRIMARY) | dedicated **serial/UART "SCP" port** on the amp (NOT USB) | the physical **Conductor remote ↔ amp** | the real runtime protocol: volume, setup-select, source, status |
| **B. USB config** (secondary) | USB CDC (VID `0x2E4F`) | **PC-Tool ↔ amp** | *enabling/configuring* the Conductor feature (opcode `0x29`/URC*/`<ATFCOND>`). This config-test is what MUTED the amp. |
| **C. USB update** (tooling) | USB CDC (VID `0x2E4F`) | **`conductor.exe` ↔ Conductor unit** | firmware updater for the Conductor (opcode `0x33` bootloader group) |

> **Transport correction (user, real-world):** the actual Conductor remote connects over the **SCP port**,
> not USB. The USB `0x29`/URC path is only the PC-Tool *enabling+configuring* the feature — and that is
> what muted the amp. Planes A and B carry (largely) the **same command semantics** but over **different
> physical links**; the amp arbitrates between them (§1).

---

## 1. Plane A — the SCP protocol (Conductor remote ↔ amp)  [PRIMARY]

### 1.1 Transport
- **A dedicated UART ("SCP") on the amp**, physically separate from the USB-CDC link. "SCP" is a
  *hardware/port* name (from real-world use); it appears **nowhere as an ASCII token** in any binary
  (grep of amp fw, Conductor fw, conductor.exe, PC-Tool = no `SCP` string). So the wire baud/pins are
  **NOT statically recoverable** — **PROVISIONAL / needs hardware or a bus probe.**
- The amp MCU is a **non-STM32 ARM Cortex-M** (peripheral registers at `0x40001400`/`0x40001418`, plus
  SCB at `0xE000ED00`; standard STM32 USART bases `0x40004400/0x40013800/…` are absent). So the SCP UART
  is one of this MCU's on-chip UARTs; exact base unconfirmed.
- **Framing over SCP: PROVISIONAL.** Because SCP commands and USB commands reach the **same dispatcher/
  router** (§1.3), the likeliest case is that SCP uses the **same asymmetric `0x42`/`0x43` + additive-sum
  framing** as USB (a shared RX→deframer→dispatcher pipeline fed by two UARTs). Not proven — no second
  framing constant was found, but no separate SCP deframer was isolated either.

### 1.2 The amp arbitrates USB vs SCP  (CONFIRMED, firmware)
The amp keeps a **"controller/remote active" flag** — the byte the dispatcher gate reads:
`FUN_0000ae30() → *(0x20000133)` (part of a controller struct based at RAM **`0x20000130`**). When a
Conductor is active on SCP this is set, and the **USB command surface is auto-restricted** to exactly the
remote group:

```
FUN_00004aec @0x4b04:  iVar7 = FUN_0000ae30();
  if (iVar7 != 0 && !(opcode∈{0x07,0x08}) && opcode!=0x13 && !(opcode∈{0x28..0x2b}))  return;  // reject
```
i.e. with a controller active, **only `{0x07, 0x08, 0x13, 0x28, 0x29, 0x2a, 0x2b}` are accepted.** That
restricted set **IS the SCP/remote command set.** (The USB-only PC-Tool paths `0x02/0x03/0x1E/0x1F` are
blocked while a controller is engaged.) Evidence: `FUN_00004aec` head (rxpath.txt); gate `FUN_0000ae30`
(returns `*0x20000133`); struct base `0x20000130` referenced by the controller-manager funcs near
`0xaef8`/`0xb444`.

Note this **physical-controller flag (`0x20000133`)** is *distinct* from the **logical remote-enable flag
(`DAT_00007e24`, set by USB `0x29 01 01`)** — two different bytes. So "a real Conductor is on SCP" and
"the PC-Tool turned on remote mode over USB" are tracked separately, though both funnel into the same
`0x28`/`0x29` router.

### 1.3 SCP command set = the `0x13` + `0x28`/`0x29` router  (CONFIRMED firmware; GET hw-confirmed)
Whether a command arrives on SCP or USB, it is handled by the same code: opcode **`0x28` = GET, `0x29` =
SET**, both routed to **`FUN_00007b3c`** (`0x2a/0x2b`→`FUN_00007708`), plus standalone **`0x13`** volume.
Router `FUN_00007b3c(payload, len, resp, dir)` — **`dir!=0`=GET(0x28), `dir==0`=SET(0x29)** (proven: the
capabilities dump and all readbacks are on `dir!=0`, and `28 xx` is the hardware-confirmed GET). Sub-op =
`payload[0]`. Decompiled this session (`FUN_00007b3c`, 1420 B):

| Sub | Wire (as SET, `0x29`) | Semantics (firmware) | The Conductor uses it to… |
|---|---|---|---|
| `0x01` | `29 01 EN` | remote-enable flag `= 1-(EN==0)`, **only if mode∈{2,5,6}** (gate `FUN_00007b1c`). GET reads it | announce it has taken control |
| `0x10` | `29 10 SRC hi lo` | **source select** (SRC≤7, requires enable). GET `10 FF`→8-source map | pick input source |
| `0x13` | `29 13 hi lo` | **volume** (16-bit). SET stores at `0x20000104+0x34` and **applies to DSP** via `DAT_00007e50(mode,0,vol+offset,0)`, floored at `DAT_0000810c`. GET returns stored 16-bit | the volume knob |
| `0x20` | `29 20 MODE` | **source/remote mode**, MODE∈{2,5,6}; stored `0x…8110+0x3c`. GET→6 bytes | set control mode |
| `0x21` | `29 21 B` | stored byte at `+0x34104+2` | misc |
| `0x40` | `29 40 NN` | **select setup NN (1-based)**: needs DSP-not-busy (`DAT_00008138(4)==0`), lock flag `*(…8110+0x94)!=1`, `FUN_00006998(NN-1)` slot-valid; then `DAT_00008140(1)` triggers apply | the setup buttons |
| `0x02` | (GET) | capabilities → `[3,1,1,1,0,2,0,1,0]` (hw-confirmed) | handshake |
| `0x03` | (GET) | 0x20-byte info block | ID |
| `0x43` | (GET) | lock/active flag | status |
| `0x50` | (GET) | telemetry: supply-V(2B)+temp(2B)+status | display |
| `0x51` | (GET) | slot-enabled map | which setups exist |

So the **Conductor's runtime job over SCP** = send `0x29 13` (volume), `0x29 40` (setup), `0x29 10`
(source), read `0x28 40/50/51` (state/telemetry/slots), gated behind `0x29 20`(mode)+`0x29 01`(enable).
This is the command set whether spoken on SCP or emulated by the PC-Tool on USB — only the physical link
differs. **The exact SCP framing/baud remain the one PROVISIONAL piece.**

### 1.4 Why it mutes, and the non-muting path  (mechanism CONFIRMED; remedy PROVISIONAL)
The mute is **inherent to the remote-mode handover**, not a wrong flag. `29 20 02` + `29 01 01` transfers
master-volume authority to the `0x13` register (`0x20000104+0x34`); until a level is reported it sits at
**0 → mute**. A *real Conductor* on SCP continuously reports its knob position via `29 13`, so it never
mutes; the PC-Tool's config *test* over USB engaged remote mode **without** reporting a level → mute.

**Non-muting control (for a host library — PROVISIONAL, no hardware):**
1. **Stay out of the remote group entirely.**
   - Setups: **`0x1F sel=9`** (`1F 00 00 09 00 <idx> 00 00`) — hardware-confirmed no-mute.
   - Volume: write the master-volume **DSP cell directly** via `0x02/0x03` — the firmware exposes remote
     volume as ordinary DSP cells: `MOD_REMOTE_CONTROL_MAINVOLUMEDC_*`, `…_SUBWOOFERVOLUMEDC_*`,
     `…_REARVOLUMEDC_*`, `…_DIGITALVOLUMEDC_*`, `MOD_MAINVOLUMEURC_*`, readback `MOD_VOLUME_READBACK_*`
     (all in `MatchM54DSP.at01.inflated.h`).
2. **If you must enable remote mode, always report a level:** immediately after `29 01 01` send
   `29 13 hi lo` with a real 16-bit volume so the master register is never left at 0.

Recommendation: path 1 (0x1F setups + direct DSP-cell volume) — no mute risk, reuses hardware-confirmed
opcodes, and never contends with a real Conductor on SCP.

---

## 2. Plane B — USB config of the Conductor feature (PC-Tool `0x29`/URC*)  [secondary]

This is the PC-Tool **enabling and configuring** the Conductor, over USB-CDC, using the very same
`0x28`/`0x29` router as §1.3 — but here it is the PC *pretending to be* the remote to set it up/test it.
Doing so engages remote mode and (with no level reported) **mutes** — see §1.4. It is NOT the runtime
protocol. The persistent config it writes lives in the setup file (§4).

`protocol.yaml`'s `0x29 set_group_REMOTE` (marked DANGEROUS) is exactly this plane. Keep using `0x1F`
for setups, and never enable the remote over USB without reporting volume.

---

## 3. Plane C — `conductor.exe`, the Conductor firmware updater (`0x33`)  [tooling]

**PE32 GUI, x86, Qt5 (MinGW), 115 KB, built 2022-05-18.** A single-window flasher (one `QProgressBar` +
status label). Not a control panel.

- **Transport:** USB CDC serial, port matched by **`vendorIdentifier()==0x2E4F`** (`fcn.00401430
  @0x401555`); same `0x42`/`0x43` framing + additive-sum checksum (framer `fcn.00401980`, checksum
  `fcn.004010d0`).
- **Firmware file:** `./firmware/App_Conductor_Release.rem` — de-XOR key **`Audiotec Fischer - conUpdater`**
  + `qUncompress` → Intel-HEX (the `.rem` firmware container; parser `fcn.00403480`). CLI
  flags `--noLog/--noWait/--createCON/--parseCON`.
- **Command group `0x33`** (state machine `fcn.00404960`; literal pool `px @0x409ffc`):

| State | TX payload | Meaning |
|---|---|---|
| `sCheckRemoteControl` | `33 00` | probe: app or bootloader? → 5-byte reply `33 00 02 SS XX` (SS: 1=app→start BL, 0=already in BL, else unknown) |
| `sStartBootloader` | `33 B0 B0 07` | force app→bootloader (magic `B0 07`) |
| `sConnectBootloader` | `33 B1` | ping bootloader |
| `sStartUpdate` | `33 B2 <4><4><2>` | send MetaData (start/len/CRC — **field split HYPOTHESIS**) |
| `sUpdateRunning` | `33 B2…`/`33 B3` | stream firmware / poll |

- **Who consumes `0x33`:** **NOT the amp** — its dispatcher `FUN_00004aec` only switches `0x00–0x2b` and
  has **no relay/forward** of unknown opcodes (verified: unmatched opcodes fall through the switch). So
  `conductor.exe` talks `0x33` **directly to the Conductor unit** (an ACO-family device that enumerates
  VID `0x2E4F` when connected for update; its own firmware `App_Conductor_Release.bin`, ARM, 18 KB,
  stripped, runs the `0x33` bootloader). It reaches the Conductor over USB, **not** over SCP.
  **PROVISIONAL:** exactly how the Conductor gets onto USB for flashing (own port vs. amp pass-through)
  is unconfirmed without hardware.

State graph: `sConnectDevice → sCheckRemoteControl → sStartBootloader → sConnectBootloader → sStartUpdate
→ sUpdateRunning → sUpdateSuccessful | sUpdateFailed`.

---

## 4. `<ATFCOND>` + `<MCV2>`/URC* config semantics  (CONFIRMED exist + real values; meanings mixed)

The `.pct6` (and standalone **`.afcc`** "ATF Conductor Config") carry the Conductor's persistent config —
what Planes A/B ultimately apply. PC-Tool describes `<MCV2>` as the global block: _"Remote Control
settings (including the CONDUCTOR configuration) … 'Dynamic Loudness Control' (DLC) … 'Remote Tone
Control' (RTC)."_ Decoded from a real file (`extracted/app/setups/UP_8BMW_PP-BMW1_7Hifi_Basic.pct6`,
`qUncompress(XOR(·,"ATFV6"))`):

### `<ATFCOND D="130520260943" Dev="50" CV="7" AV="26" PCTV="6.03.03" ACV="11">`
| Attr | ex | Meaning | Confidence |
|---|---|---|---|
| `PCTV` | 6.03.03 | PC-Tool version that wrote it | CONFIRMED |
| `Dev` | 50 | internal ACO device-type id (M5.4DSP=29, this UP8-BMW=50) — NOT the USB PID | CONFIRMED |
| `D` | 130520260943 | Conductor config date/id (reads as 13·05·2026 09:43) | HYPOTHESIS |
| `CV` | 7 | Conductor (firmware) version (cf. `"Conductor Version: "`) | HYPOTHESIS |
| `AV` | 26 | ACO/app version | HYPOTHESIS |
| `ACV` | 11 | ATF-Conductor config-schema version (cf. `"got v2 Conductor Config"`) | HYPOTHESIS |

### URC* fields in `<MCV2>` (`URCM=0 URCS=0 URCS1=0 URCS2=1 URCC1=255 URCC2=255 URCLTI1=123 URCLTI2=0`)
| Field | ex | Meaning | Confidence |
|---|---|---|---|
| `URCM` | 0 | URC master enable/mode (0=off) → maps to `0x29 01/0x20` | HYPOTHESIS (strong) |
| `URCS` | 0 | enable "Setup Switch" (`checkURCSwitch`) — let remote switch setups | HYPOTHESIS (strong) |
| `URCS1` | 0 | setup index for button/pos 1 ("green", `cBURCGreen`), 0-based | HYPOTHESIS (strong) |
| `URCS2` | 1 | setup index for button/pos 2 ("red", `cBURCRed`) | HYPOTHESIS (strong) |
| `URCC1`/`URCC2` | 255 | colour/brightness for group 1/2 (LED config) | HYPOTHESIS |
| `URCLTI1`/`URCLTI2` | 123/0 | group-1/2 LED intensity or timing (`LTI`) | HYPOTHESIS (weak) |

Supporting PC-Tool symbols: class `dcmConductor`; UI `gBDCMRC_URC(_Controls/_Image)`, `gBDCMURCSelect`,
`checkURCSwitch`, `cBURCGreen`, `cBURCRed`, `cBLoadConductor`, `widgetConductor`; menus `URC Setup Switch
Configuration`, `Open Conductor Config…`, `Reset Conductor Configuration`; file type `ATF Conductor Config
(*.afcc)`; loader keyed on `Global`/`Volume`/`TC` XML nodes; DSP cells `DSP1_MOD_URC_FUNCTION_
{MAIN,MASTER,SUB,BEC1,BEC2,OPTO}VOLUMEVALUE`, `…SOURCESELECTVALUE`. PC-Tool string: _"Allows to chose
which sound setups should be switched when using the URC Remote Control."_

---

## 5. Adversarial refutation notes
- **"conductor.exe drives volume/setup" — REFUTED.** Only serial commands = `0x33` bootloader group; no
  `0x29/0x13/0x40` anywhere; UI is a progress bar. It is a flasher, full stop.
- **"amp bridges `0x33` to the Conductor" — not supported.** `0x33` is absent from the amp dispatcher
  switch (`0x00–0x2b` only) and there is **no default/relay branch** — unmatched opcodes fall through. So
  `0x33` is consumed by the **Conductor unit itself**, not relayed by the amp. (The `33 B1` byte-hit at
  amp file-offset 0x3dcb is below load base 0x4000 = data, coincidence.)
- **SCP vs USB command set — re-derived from the gate.** The dispatcher restricts to exactly
  `{0x07,0x08,0x13,0x28..0x2b}` when the controller flag is set, and that flag (`0x20000133`) is a
  *separate* byte from the USB remote-enable flag (`DAT_00007e24`). Two independent facts (the restricted
  set == the remote router opcodes; the separate physical-vs-logical flags) both say: SCP carries the
  remote command group, arbitrated against USB by the amp.
- **GET/SET direction — re-derived, not assumed:** capabilities/telemetry/slot reads all sit on `dir!=0`
  and `28 xx` is the hardware-confirmed GET ⇒ `dir!=0`=GET(0x28), `dir==0`=SET(0x29); consistent with the
  state-mutating `0x40` SET being on `dir==0`.
- **Mute cause — two independent derivations agree** (hardware log + the `0x13` apply-path writing the
  master register whose unreported value is 0): mute = volume-authority handover, not a bad flag.
- **`Dev`≠PID re-confirmed:** ATFCOND `Dev="50"` (UP8-BMW) is the internal device-type id, not `0x2008`.

---

## 6. Confirmed vs hypothesis vs needs-hardware

**CONFIRMED (static/firmware/cross-checked):**
- Three-plane model: SCP (Conductor↔amp, dedicated UART), USB-config (PC-Tool `0x29`/URC), USB-update
  (`conductor.exe` `0x33`).
- Amp arbitration: controller-active flag `0x20000133` (struct `0x20000130`) restricts USB to
  `{0x07,0x08,0x13,0x28..0x2b}` when a Conductor is active — that set is the SCP command set.
- SCP/remote command semantics (`FUN_00007b3c`): `0x13` volume-applies-to-DSP, `0x10` source, `0x20`
  mode∈{2,5,6}, `0x01` enable (mode-gated), `0x40` setup(1-based)+preconds, `0x50` telemetry, `0x51`
  slots. GET side hardware-confirmed 2026-08-25.
- Mute = remote-mode master-volume handover with no reported level.
- `conductor.exe` = Conductor updater; `0x33` group; `.rem`/`conUpdater` format; VID-2E4F CDC; `0x42`
  framing. `0x33` not handled by the amp.
- `<ATFCOND>`/`<MCV2>`/URC* real values; `PCTV`/`Dev` meanings.

**HYPOTHESIS:** `33 B2` metadata field split; ATFCOND `D/CV/AV/ACV`; URC* meanings (URCM/URCS/URCS1/URCS2
high-confidence, URCC*/URCLTI* lower).

**NEEDS HARDWARE / CAPTURE (PROVISIONAL):**
- **SCP wire details:** physical port/pins, baud, and whether SCP framing == USB `0x42`/`0x43` or a
  variant. (Not recoverable statically — no `SCP` token, non-standard MCU peripherals.) A bus probe on
  the amp's remote connector, or a Conductor-fw disassembly with a known base, would settle it.
- How the Conductor reaches USB for `conductor.exe` flashing (own CDC port vs amp pass-through).
- Non-muting **direct DSP-cell volume write** (`MOD_REMOTE_CONTROL_MAINVOLUMEDC` via `0x02/0x03`):
  address/encoder/round-trip unverified.
- Exact URC*→wire mapping (whether config is pushed via `0x29 20/01/10`, `0x1E/0x1F`, or only baked into
  `.pct6`/`.afcc`). A PC-Tool USB capture would settle it.

---

## 7. Evidence index (function addresses)
**`conductor.exe` (PE, base 0x400000):** `fcn.00404960` state machine · `fcn.00401980` framer ·
`fcn.004010d0` checksum · `fcn.00401430` port scan (VID `0x2e4f @0x401555`) · `fcn.00403480` `.rem` parser ·
payload thunks `fcn.00402ac0`(33 00)/`fcn.004029a0`(33 B0 B0 07)/`fcn.00402b50`(33 B1)/`fcn.00402a30`+
`fcn.00402910`(33 B3) · `.rdata` pool `@0x409ffc` · response parse `@0x404d3e`.

**Amp fw (`App_M5.4DSP_Release.bin`, ARM, base 0x4000):** `FUN_00004aec` dispatcher (gate `@0x4b04`;
`0x28/0x29`→`FUN_00007b3c`, `0x2a/0x2b`→`FUN_00007708`) · `FUN_00007b3c` remote router · `FUN_00007b1c`
mode gate (∈{2,5,6}) · `FUN_0000ae30` controller-active gate (`*0x20000133`) · controller struct
`0x20000130` (mgr funcs near `0xaef8`) · `FUN_00006998` setup setter · `0x1F`→`FUN_00008be8` case 9.
RAM: physical-controller flag `0x20000133`, USB remote-enable `DAT_00007e24`, volume `0x20000104+0x34`,
mode `…8110+0x3c`, lock `…8110+0x94`. MCU peripheral base `0x40001400` (non-STM32).

**Conductor fw (`App_Conductor_Release.bin`, ARM, 18 KB, stripped):** runs the `0x33` bootloader consumed
by `conductor.exe`; base/UART unconfirmed.

**Config:** `MatchM54DSP.at01.inflated.h` (`MOD_REMOTE_CONTROL_*VOLUMEDC_*`, `MOD_MAINVOLUMEURC_*`,
`MOD_VOLUME_READBACK_*`) · decoded `.pct6` `<ATFCOND>`/`<MCV2>` · PC-Tool URC/`dcmConductor` symbols.
