# Validating your device (community hardware validation)

`atf-dsp-sdk` ships parameter maps for the whole Audiotec Fischer ACO range, but
only the **MATCH M 5.4DSP** has been checked against real hardware. Every other
model is *expected* to work (shared firmware and encoders) but is unverified.
This is how you can help close that gap for **your** amp — safely.

You do **not** need to be a developer. The `atf-dsp-sdk validate` command does the
work and writes a report file you paste into a GitHub issue.

> ⚠️ **Safety first.** Writing DSP parameters can be loud and can damage speakers.
> The default validation is **read-only**. The optional write test snapshots and
> restores every cell, but before running it: **turn amp gain down or disconnect
> the speakers**, and select a **scratch setup slot** (not a tune you care about).
> Never run write tests on a setup you want to keep.

## What "validation" actually proves

Two independent checks, because they close different gaps:

- **Direction A — the PC-Tool oracle (safe, no amp writes).** You build a known
  setup *in the PC-Tool*, save it as a `.pct6`, and the SDK checks that its decode
  matches the values you entered. Because the PC-Tool produced the bytes, this
  proves our encoders are **semantically correct** — the strongest check, and it
  never touches the amp. **Please do this one if you can.**
- **Direction B — SDK writes → SDK reads (opt-in, gated).** The SDK writes a test
  tuning to the amp and reads it back. This proves writes persist and encode/decode
  round-trip on real silicon, but it uses our encoders on both ends, so it can't
  prove semantics on its own. It writes, then restores — but it *does* write.

## Install

```bash
pip install -e .
```

Find your amp's serial port: `atf-dsp-sdk list` (it flags ATF devices). On macOS
it's a `/dev/cu.usbmodem*`; on Windows a `COMx`.

## Step 1 — read-only probe (everyone)

```bash
atf-dsp-sdk validate --json report.json
```

This connects, identifies the model / firmware / sample rate, reads back a few
live cells to confirm the read+decode path works, and writes `report.json`. No
writes to the amp. Attach `report.json` to your issue even if you do nothing else
— it confirms the model enumerates and its map loads.

## Step 2 — Direction A, the PC-Tool oracle (safe, high value)

1. Print the exact values to enter:
   ```bash
   atf-dsp-sdk validate --print-vector > vector.txt
   ```
2. In the DSP PC-Tool (demo mode is fine — **no amp needed**), enter those values
   by hand: per output the gain / mute / delay / EQ bands / crossover; per virtual
   channel the gain / mute / EQ; input EQ. Then **File → Save As** → `oracle.pct6`.
3. Check it:
   ```bash
   atf-dsp-sdk validate --pct6 oracle.pct6 --json report.json
   ```
   Green means the PC-Tool encoded the same values the SDK decodes — your model's
   encoders are semantically correct.

You don't have to enter *every* value — even a partial setup validates the fields
you did enter. Note in your issue what you covered.

## Step 3 — Direction B, the write test (optional, gated)

**Speakers off / gain down / scratch setup selected.** Then:

```bash
atf-dsp-sdk validate --write --json report.json
```

It prints a warning and asks you to type `WRITE` to proceed (use `--yes` to skip
the prompt in a script). It writes the test vector, reads every cell back within
tolerance, and restores the original values.

## Step 4 — submit

Open a **Hardware validation report** issue (Issues → New issue → that template)
and paste the contents of `report.json`, plus:

- your model and firmware version (shown in the report),
- which steps you ran (A / B / read-only),
- anything odd you heard or saw.

We add confirmed models to [validation-status.md](validation-status.md).

## What the report contains

`report.json` is metadata only — SDK version, model, PID, firmware, sample rate,
platform, timestamp — plus per-control pass/fail with expected-vs-actual deltas.
It contains **no personal data** and no vendor files. Skim it before posting if
you like.

## FAQ

- **Is the read-only probe safe?** Yes — it only reads.
- **Will the write test wreck my tune?** It restores every cell it touches, but
  run it on a scratch setup and keep gain low regardless.
- **My model isn't detected / has no channel labels.** Raw read/write still works;
  the fluent channel model needs a per-model `.channels.yaml` overlay (only the
  M 5.4DSP ships one so far). See [Adding another model](../README.md#regenerating-or-adding-a-model).
- **BRAX 96 vs 192 kHz?** Same USB PID, so pass `--model "BRAX DSP (96 kHz)"` if
  your firmware is the 96 kHz image; the default is 192 kHz.
