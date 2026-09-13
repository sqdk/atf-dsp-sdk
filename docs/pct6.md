# `acodsp.pct6` — reading & writing DSP PC-Tool setup files

`acodsp/pct6.py` turns an encrypted DSP PC-Tool `.pct6` / `.afpx` setup file into a
structured `Setup` model, writes it back (Phase D), and applies it onto a connected
`Device` (dry-run by default). The crypto/container scheme is the CONFIRMED one from
`docs/pct6-format.md` (Phases A+B) — this module ports it and layers the Phase C model
on top.

## Layers

1. **Codec (device-agnostic).** `plaintext_xml = qUncompress(XOR(whole_file, KEY))`.
   - `decrypt(bytes) -> (variant, xml)` tries plain `qCompress`, then XOR with `ATFV6`
     (`.pct6`) and `ATF` (`.afpx`), then a raw-XML fallback.
   - `encrypt(xml, variant) -> bytes` is the exact inverse (`XOR(qCompress(xml), KEY)`).
   - `q_compress` / `q_uncompress` are Qt's format: 4-byte **big-endian** length + a raw
     zlib stream.
   - Password-protected `ATFV6P` files use an unconfirmed `QCryptographicHash` transform
     and raise `Pct6PasswordError` — no guessing.

2. **`Setup` model.**
   - `Setup.load(path)` → decrypt + parse; `Setup.from_xml(bytes)` parses decrypted XML.
   - `Setup.metadata` (`SetupMetadata`): `pid` (`<ATF Dev>` — the tool's INTERNAL device
     id, e.g. 50; **not** the USB PID), `version` (`V`), `filename` (`FN`), `outs`, `ins`,
     plus every root attribute in `.raw`.
   - `Setup.outputs` / `Setup.inputs` (`Channel`): `index`, `name` (friendly text label if
     the setup carries one — the shipped sample carries none), `name_code` (`CN`),
     `gain_db` (from the `<Vol L>` linear level → dB), `mute` (`MT`), `enabled` (`CE`),
     `eq_bypass` (`EqBy`), `eq_bands`, `hp_index`/`lp_index` (`HPi`/`LPi` crossover band
     indices), `delay_raw` (the `<T>` child — phase/delay, unit unconfirmed → kept raw),
     and full `raw` attrs.
   - `EqBand`: `freq`, `gain_db`, `q`, `kind` (mapped filter kind or `None`), `raw_type`
     (original `<Fil T>` code), `bypass`, `index`, `raw`. Bands are preserved **low→high**
     frequency (band 0 = 25 Hz).
   - `Setup.routing` (`Routing` → `RoutingBlock` list): each `<R>` block's `ins`/`outs`/
     `offs`/`aom` + a `gains` dict of `G<n>` linear mix cells.
   - `Setup.save(path)` / `Setup.to_xml()` re-encrypt the retained tree — the exact
     inverse of load for an unmodified setup (round-trip validated in tests).

3. **`Device.apply_setup(setup, dry_run=True, force=False)`** (also `pct6.apply_setup`).
   Maps setup outputs/inputs onto the device channel model **by index → letter** and
   writes **gain + peaking EQ** through the hardware-confirmed typed API (crossover/delay/
   routing are gated encoders and are skipped here). Safety:
   - `dry_run=True` (default) never transmits to real hardware. On a dry-run link the 0x03
     SafeLoad frames are still *built* (inspect `device.link.sent`); on a real link it is
     plan-only. A real apply is the explicit `dry_run=False` opt-in and snapshots the gain
     cells first.
   - **Device-PID match:** `setup.metadata.pid` vs the connected device PID; on mismatch
     it raises `PidMismatchError` unless `force=True`.

## `<Fil T>` filter-type codes

| Code | Kind | Confidence |
|---|---|---|
| `1` | peaking (graphic band, fixed ISO freq, Q=4.3) | confirmed |
| `17` | peaking (parametric) | confirmed |
| `9` | lowpass (crossover) | inferred from `LPi` index correspondence |
| `10` | highpass (crossover) | inferred from `HPi` index correspondence |

Shelf codes (low/high shelf) and any others are **not** present in the sample and are
left unmapped: `EqBand.kind is None` with `raw_type` preserved. `Setup.unmapped_filter_types()`
lists any encountered codes we could not map (empty for the shipped sample).

## Usage

```python
from acodsp import Setup, Device, Link, ParamMap

setup = Setup.load("UP_8BMW_PP-BMW1_7Hifi_Basic.pct6")
print(setup.summary())
print(setup.outputs[0].eq_bands[0])          # 25 Hz graphic band

# round-trip (Phase D)
setup.save("copy.pct6")

# dry-run apply onto a (dry-run) device — no hardware
dev = Device(link=Link(dry_run=True), param_map=ParamMap.load("MatchM54DSP"),
             model="MATCH M 5.4DSP")
res = dev.apply_setup(setup, dry_run=True, force=True)   # force: Dev=50 != device PID
print(res)                                                # ApplyResult(writes=..., frames=...)
```
