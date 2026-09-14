# Hardware validation status

Coverage of the ACO model range. **Param map** ships for every model; the
**channel overlay** (fluent labelled API) and **hardware status** are what
community validation fills in. See
[validating-your-device.md](validating-your-device.md) to contribute a report.

Legend — Hardware status: ✅ validated on real hardware · 🟡 round-trips only
(Direction B on the amp, semantics unconfirmed) · ⬜ untested. Maintainers update
a row from a submitted [hardware-validation issue](../../issues?q=label%3Ahardware-validation).

| Model | Sample rate | Param map | Channel overlay | Hardware status |
|---|---|---|---|---|
| BRAX DSP | 192 kHz | yes | — | ⬜ untested |
| BRAX DSP (96 kHz) | 96 kHz | yes | — | ⬜ untested |
| HELIX AMPLIFY 206 DSP | 48 kHz (assumed) | yes | — | ⬜ untested |
| HELIX DSP MINI | 48 kHz (assumed) | yes | — | ⬜ untested |
| HELIX DSP MINI MK2 | 48 kHz (assumed) | yes | — | ⬜ untested |
| HELIX DSP PRO MK3 | 48 kHz (assumed) | yes | — | ⬜ untested |
| HELIX DSP ULTRA | 48 kHz (assumed) | yes | — | ⬜ untested |
| HELIX DSP ULTRA S | 48 kHz (assumed) | yes | — | ⬜ untested |
| HELIX DSP ULTRA XT | 48 kHz (assumed) | yes | — | ⬜ untested |
| HELIX DSP.3 | 48 kHz (assumed) | yes | — | ⬜ untested |
| HELIX DSP.3S | 48 kHz (assumed) | yes | — | ⬜ untested |
| HELIX ISM 400.2DSP | 48 kHz (assumed) | yes | — | ⬜ untested |
| HELIX M FOUR DSP | 48 kHz (assumed) | yes | — | ⬜ untested |
| HELIX M SIX DSP | 48 kHz (assumed) | yes | — | ⬜ untested |
| HELIX P SIX DSP ULTIMATE | 48 kHz (assumed) | yes | — | ⬜ untested |
| HELIX V EIGHT DSP MK2 | 48 kHz (assumed) | yes | — | ⬜ untested |
| HELIX V EIGHT DSP ULTIMATE | 48 kHz (assumed) | yes | — | ⬜ untested |
| HELIX V EIGHTEEN DSP | 48 kHz (assumed) | yes | — | ⬜ untested |
| HELIX V TWELVE DSP | 48 kHz (assumed) | yes | — | ⬜ untested |
| HELIX V TWELVE DSP MK2 | 48 kHz (assumed) | yes | — | ⬜ untested |
| MATCH M 5.4DSP | 48 kHz | yes | yes | ✅ validated (2026-08) |
| MATCH M 5DSP MK2 | 48 kHz (assumed) | yes | — | ⬜ untested |
| MATCH PP 86DSP MK2 | 48 kHz (assumed) | yes | — | ⬜ untested |
| MATCH UP 10DSP | 48 kHz (assumed) | yes | — | ⬜ untested |
| MATCH UP 10DSP - 24V | 48 kHz (assumed) | yes | — | ⬜ untested |
| MATCH UP 10DSP MK2 | 48 kHz (assumed) | yes | — | ⬜ untested |
| MATCH UP 4DSP | 48 kHz (assumed) | yes | — | ⬜ untested |
| MATCH UP 6DSP | 48 kHz (assumed) | yes | — | ⬜ untested |
| MATCH UP 6DSP MK2 | 48 kHz (assumed) | yes | — | ⬜ untested |
| MATCH UP 8BMW | 48 kHz (assumed) | yes | — | ⬜ untested |
| MATCH UP 8BMW MK2 | 48 kHz (assumed) | yes | — | ⬜ untested |
| MATCH UP 8DSP | 48 kHz (assumed) | yes | — | ⬜ untested |
| MATCH UP 8DSP MK2 | 48 kHz (assumed) | yes | — | ⬜ untested |

"48 kHz (assumed)" means the rate is the SDK's fallback, not confirmed for that
model (the MATCH/HELIX line is understood to be 48 kHz, but only the M 5.4DSP is
bench-confirmed). BRAX rates come from the device-file name and are trustworthy.
