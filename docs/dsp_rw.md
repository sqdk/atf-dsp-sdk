# DSP read/write primitives (0x02 / 0x03)

Source of truth: `../protocol.yaml` (`dsp_rw`, status: confirmed — firmware +
two adversarial reviews). Part 1 implements the raw encoders; value-encoding
semantics (fixed-point, biquads) are Part 2.

## Word size & byte order
- **Word size: 4 bytes (32-bit).** One `.at01` address == one 32-bit DSP word.
- **Byte order: big-endian** for BOTH the 16-bit address and the data words. The
  firmware does not byte-swap; the host must emit big-endian (matches the ADAU145x
  native SPI).
- **Instance:** honored when `(instance & 7) == 0`. Use `0`.

## 0x02 — DSP read
```
02 <instance> <addr_hi> <addr_lo> [N pad bytes]
```
The number of trailing pad bytes sets the read size (`N = payload_len - 4`). The
response echoes the 4-byte header then N value bytes. Encoder: `encode_read_param`.

## 0x03 — DSP write
```
03 <instance> <flag> <addr_hi> <addr_lo> <data BE...>
```
- `flag == 0xFF` -> **direct** write.
- `flag != 0xFF` -> **SafeLoad**, but only when total payload length `< 0x1a` (26);
  otherwise the firmware falls back to the direct path.

Encoder: `encode_write_param` (`safeload=True` -> flag `0x00`; `safeload=False` ->
flag `0xFF`). The SafeLoad *block-building* (28-byte / 7-word block, address at
bytes [22]/[23], NUM-words trigger at [27]) is Part 2's `safeload.py`; Part 1 only
emits the raw 0x03 command.

## Hardware confirmation (pending)
Round-trip read/write on hardware is Part 1 task 7; the opt-in `tests/test_hardware.py`
covers identify, get/set/restore setup via 0x1F, a read, and a same-value write
round-trip that restores state. Not yet exercised live at time of writing — the
`confirmed` status above is firmware + review.
