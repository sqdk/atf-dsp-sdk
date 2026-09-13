"""Offline tests for the CHANNEL-MODEL abstraction layer (acodsp.channels).

Covers: data-driven topology (counts/shapes from discovery, never hardcoded),
name resolution to real .at01 params, the overlay-driven band->(block,stage) map
(and that flipping the overlay flips resolution), dry-run SafeLoad emission, and the
provisional flags. All offline — no hardware.
"""
from __future__ import annotations

import copy

import pytest

from acodsp import encoding, protocol
from acodsp.channels import ChannelModel, ChannelModelError
from acodsp.device import Device
from acodsp.params import ParamMap
from acodsp.transport import Link, parse_frames


@pytest.fixture()
def pm() -> ParamMap:
    return ParamMap.load("MatchM54DSP")


@pytest.fixture()
def model(pm: ParamMap) -> ChannelModel:
    return ChannelModel.load("MatchM54DSP", pm)


def _dry_device(pm: ParamMap) -> Device:
    return Device(link=Link(dry_run=True), param_map=pm, model="MATCH M 5.4DSP")


def _tx_frames(link: Link):
    return [f for f in parse_frames(link.sent[-1]) if f.direction == "TX"]


def _stage_writes(link: Link):
    """addr -> 20-byte payload for every SafeLoad (0x03) stage write across ALL sent buffers
    (each crossover stage is its own frame, so a full write spans several buffers)."""
    m = {}
    for buf in link.sent:
        for f in parse_frames(buf):
            if f.direction == "TX" and f.payload[:1] == b"\x03" and len(f.payload[5:]) == 20:
                m[f.payload[3] << 8 | f.payload[4]] = f.payload[5:]
    return m


# --- topology (discovered, data-driven) --------------------------------------
def test_topology_counts_match_discovery(model: ChannelModel):
    assert model.input_letters == ["A", "B", "C", "D"]                      # 4 inputs
    assert model.output_letters == list("ABCDEFGHI")                        # 9 outputs
    assert model.eq_blocks["EQ1"] == {"instances": 9, "bands": 15}
    assert model.eq_blocks["EQ2"] == {"instances": 9, "bands": 16}
    assert model.topology.get("routing_matrix")                             # matrix present
    # top-level shim delegates to the virtual->output matrix (9x9)
    assert model.routing.rows == 9 and model.routing.cols == 9


# --- name resolution (every resolved name must exist in the map) -------------
def test_output_gain_resolves_to_outputmute_cell(model: ChannelModel):
    # hw-confirmed: output gain/level = the OUTPUTMUTE gain/mute cell, index = channel position.
    ref = model.output("C").resolve_gain()
    assert ref.name == "MOD_OUTPUTMUTE_ALG0_MUTE2"          # C = index 2
    assert ref.name == model.output("C").resolve_mute().name  # gain + mute share the cell
    assert ref.name in model.params and ref.provisional is False


def test_virtual_gain_resolves_to_vcp_cell(model: ChannelModel):
    # hw-confirmed: virtual gain/mute = VCP (Virtual Channel Processing) cell.
    ref = model.virtual("A").resolve_gain()
    assert ref.name == "MOD_VCPMUTE_ALG0_MUTE0"
    assert model.virtual("H").resolve_gain().name == "MOD_VCPMUTE_ALG0_MUTE7"  # H = index 7
    assert ref.name in model.params


def test_output_delay_resolves(model: ChannelModel):
    ref = model.output("A").resolve_delay()
    assert ref.name == "MOD_DELAY__PHASE_SWITCH_DELAYA_DELAYAMT"
    assert ref.name in model.params


def test_input_eq_band_resolves_to_five_stage3_coeffs(model: ChannelModel):
    ref = model.input("A").resolve_eq_band(3)
    assert ref.names == [
        "MOD_INPUT_EQ_LINE_INPUTEQA_ALG0_STAGE3_B2",
        "MOD_INPUT_EQ_LINE_INPUTEQA_ALG0_STAGE3_B1",
        "MOD_INPUT_EQ_LINE_INPUTEQA_ALG0_STAGE3_B0",
        "MOD_INPUT_EQ_LINE_INPUTEQA_ALG0_STAGE3_A2",
        "MOD_INPUT_EQ_LINE_INPUTEQA_ALG0_STAGE3_A1",
    ]
    assert ref.base == ref.names[0]
    assert all(n in model.params for n in ref.names)
    assert ref.provisional is False


def test_output_eq_band_resolves_to_cascaded_eq1_eq2(model: ChannelModel):
    # CONFIRMED (screenshot: Outputs>Equalization, 30-band EQ): output EQ = EQ1+EQ2 cascaded.
    # band 2 -> EQ1 instance 2 stage 2 (Output B -> instance suffix "_2").
    r = model.output("B").resolve_eq_band(2)
    assert (r.block, r.stage) == ("EQ1", 2)
    assert r.base == "MOD_EQUALIZER_EQ1_2_ALG0_STAGE2_B2"
    # band 17 -> EQ2 instance 2 stage 2 (17 - 15 = stage 2 of the second block).
    r2 = model.output("B").resolve_eq_band(17)
    assert (r2.block, r2.stage) == ("EQ2", 2)
    assert r2.base == "MOD_EQUALIZER_EQ2_2_ALG0_STAGE2_B2"
    assert all(n in model.params for n in r.names + r2.names)
    assert r.base_addr == model.params.addr(r.base)


# --- band -> (block, stage) mapping is overlay-driven ------------------------
def test_output_band_count_and_mapping_follow_overlay_default(model: ChannelModel):
    # CONFIRMED: output = EQ1(15) + EQ2(16) cascaded = 31 physical stages
    # (bands 0..29 are the 30 UI graphic bands; band 30 = extra physical stage / Fine EQ).
    assert model.output("A").eq_band_count == 31
    r0 = model.output("A").resolve_eq_band(0)
    assert (r0.block, r0.stage) == ("EQ1", 0)
    r15 = model.output("A").resolve_eq_band(15)
    assert (r15.block, r15.stage) == ("EQ2", 0)


def test_flipping_overlay_changes_resolution(model: ChannelModel):
    # Flippability still works: moving EQ1 off 'output' (its old virtual-EQ hypothesis)
    # is a one-line overlay change -> output then resolves to EQ2 only.
    overlay = copy.deepcopy(model.overlay)
    overlay["eq_blocks"]["EQ1"]["assigned_to"] = "virtual"
    flipped = ChannelModel(model.model_name, model.params, model.topology, overlay)

    out = flipped.output("A")
    assert out.eq_band_count == 16                            # EQ2 only now
    r0 = flipped.output("A").resolve_eq_band(0)
    assert (r0.block, r0.stage) == ("EQ2", 0)
    assert r0.base == "MOD_EQUALIZER_EQ2_ALG0_STAGE0_B2"
    assert all(n in flipped.params for n in r0.names)


# --- dry-run SafeLoad emission -----------------------------------------------
def test_output_gain_emits_one_0x03_write_of_minus6db(pm: ParamMap):
    dev = _dry_device(pm)
    dev.model.output("A").gain(-6)

    assert len(dev.link.sent) == 1
    (frame,) = _tx_frames(dev.link)
    # firmware-SafeLoad frame: 03 <inst> <flag!=0xFF> <ah> <al> <data...>
    assert frame.payload[0] == protocol.OP_DSP_WRITE and frame.payload[2] != 0xFF
    gain_addr = dev.model.output("A").resolve_gain().addr
    assert ((frame.payload[3] << 8) | frame.payload[4]) == gain_addr
    data = frame.payload[5:]
    assert len(data) == 4                                     # one data word
    assert data == bytes.fromhex("00804DCE") == encoding.gain_db_to_raw(-6).to_bytes(4, "big")


def test_output_eq_band_emits_single_safeload_frame_at_base(pm: ParamMap):
    dev = _dry_device(pm)
    base_addr = dev.model.output("B").resolve_eq_band(2).base_addr

    n0 = len(dev.link.sent)
    dev.model.output("B").eq_band(2, f=1000, Q=1.0, gain_db=-3.0, kind="peaking")

    assert len(dev.link.sent) - n0 == 1                       # exactly one frame
    (frame,) = _tx_frames(dev.link)
    assert frame.payload[0] == protocol.OP_DSP_WRITE          # 0x03 SafeLoad burst
    assert frame.payload[2] != 0xFF                           # firmware SafeLoad selector
    assert ((frame.payload[3] << 8) | frame.payload[4]) == base_addr   # resolved base address
    assert len(frame.payload[5:]) == 20                       # 5 biquad coeffs (5 x 4 bytes)


# --- graphic-EQ helpers: band_frequency + set_band_gain (by number) ----------
def test_band_frequency_returns_iso_centres(model: ChannelModel):
    for ch in (model.output("A"), model.virtual("A")):
        assert ch.band_frequency(0) == 25.0
        assert ch.band_frequency(16) == 1000.0
        assert ch.band_frequency(29) == 20000.0
    # band 30 exists physically on output (31 stages) but has no graphic ISO frequency.
    with pytest.raises(ChannelModelError):
        model.output("A").band_frequency(30)
    with pytest.raises(ChannelModelError):
        model.output("A").band_frequency(99)


def test_set_band_gain_equals_eq_band_at_iso_freq_and_default_q(pm: ParamMap):
    # set_band_gain(i, dB) must be exactly eq_band(i, f=ISO[i], Q=default_q, gain_db=dB).
    a = _dry_device(pm); b = _dry_device(pm)
    a.model.output("A").set_band_gain(16, -4.0)               # band 16 = 1 kHz
    b.model.output("A").eq_band(16, f=1000.0, Q=4.318, gain_db=-4.0, kind="peaking")
    assert a.link.sent == b.link.sent and len(a.link.sent) == 1
    # explicit Q override is honoured.
    c = _dry_device(pm); d = _dry_device(pm)
    c.model.output("A").set_band_gain(16, -4.0, Q=2.0)
    d.model.output("A").eq_band(16, f=1000.0, Q=2.0, gain_db=-4.0, kind="peaking")
    assert c.link.sent == d.link.sent


def test_set_band_gain_is_chainable_and_scoped(pm: ParamMap):
    dev = _dry_device(pm)
    from acodsp.channels import OutputChannel
    assert isinstance(dev.model.output("A").set_band_gain(0, 2.0), OutputChannel)
    # virtual graphic channels have it; a pass-through (G) still refuses (no EQ).
    dev.model.virtual("A").set_band_gain(0, 1.0)
    with pytest.raises(ChannelModelError):
        dev.model.virtual("G").set_band_gain(0, 1.0)
    # input EQ is not a fixed-frequency graphic EQ -> no set_band_gain sugar.
    assert not hasattr(dev.model.input("A"), "set_band_gain")


def test_fluent_chain_and_delay(pm: ParamMap):
    dev = _dry_device(pm)
    # gain + delay chain must return the channel and emit two frames.
    # delay encoder is status 'hypothesis' -> explicit unsafe=True opt-in required.
    dev.model.output("A").gain(-3).delay_ms(2.5, unsafe=True)
    assert len(dev.link.sent) == 2


def test_confirmed_encoders_write_without_unsafe(pm: ParamMap):
    """All encoders (gain/mute/delay/eq/crossover/routing) are now hardware-confirmed,
    so the typed channel API writes without any unsafe=True opt-in."""
    dev = _dry_device(pm)
    dev.model.output("A").delay_ms(2.5)
    dev.model.output("A").crossover(hp=100, characteristic="butterworth", slope=12)
    dev.model.routing.virtual_to_output.set("A", "A", 0.0)
    assert len(dev.link.sent) >= 3                         # each emitted a frame, none raised


# --- provisional flags -------------------------------------------------------
def test_provisional_flags(model: ChannelModel):
    assert model.output("A").resolve_gain().provisional is False       # confirmed physical
    assert model.output("B").resolve_eq_band(0).provisional is False   # output EQ CONFIRMED (screenshot)
    assert model.input("A").resolve_eq_band(0).provisional is False    # input EQ confirmed
    # virtual-channel EQ is now CONFIRMED (screenshot: 'Virtual' tab) -> non-provisional.
    assert model.virtual("A").resolve_eq_band(0).provisional is False


# --- virtual channels (CONFIRMED: 'Virtual' tab, 30-band EQ = 2x15 GLOBAL_EQ) ---------
def test_virtual_channels_present_incl_g_with_labels(model: ChannelModel):
    virtuals = model.virtuals()
    # 9 virtual channels A..I; G is "Pass Through 1" (present in routing, no EQ tab).
    assert [v.key for v in virtuals] == ["A", "B", "C", "D", "E", "F", "G", "H", "I"]
    # canonical label = PC-Tool letter pointer ("Virtual A"); friendly names are optional setup_label.
    assert {v.key: v.label for v in virtuals} == {k: f"Virtual {k}" for k in "ABCDEFGHI"}
    setup_labels = {v.key: v.setup_label for v in virtuals}
    assert setup_labels == {
        "A": "Front L", "B": "Front R", "C": "Rear L", "D": "Rear R",
        "E": "Front Center", "F": "Rear Center", "G": "Pass Through 1",
        "H": "Subwoofer 1", "I": "Subwoofer 2",
    }
    # selection still works by letter OR by the friendly setup label.
    assert model.virtual("Subwoofer 1").key == "H"
    assert model.virtual("G").pass_through is True
    assert model.virtual("G").eq_band_count == 0


def test_virtual_g_pass_through_has_no_eq(model: ChannelModel):
    # G is pass-through: EQ operations raise a clear ChannelModelError.
    with pytest.raises(ChannelModelError, match="pass-through channel 'G' has no EQ"):
        model.virtual("G").eq_band(0, f=100, Q=1.0, gain_db=0.0)
    with pytest.raises(ChannelModelError):
        model.virtual("G").resolve_eq_band(0)


def test_virtual_eq_band_count_is_30(model: ChannelModel):
    assert model.virtual("A").eq_band_count == 30                       # 2 x 15-stage sub-blocks


def test_virtual_a_resolves_two_global_eq_subblocks(model: ChannelModel):
    r0 = model.virtual("A").resolve_eq_band(0)
    assert r0.base == "MOD_GLOBAL_EQ_FRONT_GLOBALEQFRONTL1_ALG0_STAGE0_B2"
    assert (r0.block, r0.stage) == ("FRONT_GLOBALEQFRONTL1", 0)
    r15 = model.virtual("A").resolve_eq_band(15)                        # crosses into 2nd sub-block
    assert r15.base == "MOD_GLOBAL_EQ_FRONT_GLOBALEQFRONTL2_ALG0_STAGE0_B2"
    assert (r15.block, r15.stage) == ("FRONT_GLOBALEQFRONTL2", 0)
    assert all(n in model.params for n in r0.names + r15.names)


def test_virtual_h_sub_naming_quirk(model: ChannelModel):
    # Vendor naming quirk: virtual H's FIRST sub-block is spelled ...SUB_GLOBALEQFRONTL1.
    r0 = model.virtual("H").resolve_eq_band(0)
    assert r0.base == "MOD_GLOBAL_EQ_SUB_GLOBALEQFRONTL1_ALG0_STAGE0_B2"
    r15 = model.virtual("H").resolve_eq_band(15)
    assert r15.base == "MOD_GLOBAL_EQ_SUB_GLOBALEQSUBL2_ALG0_STAGE0_B2"
    assert all(n in model.params for n in r0.names + r15.names)


def test_virtual_selectable_by_letter_or_label(model: ChannelModel):
    by_letter = model.virtual("H")
    by_label = model.virtual("Subwoofer 1")
    assert by_letter.key == by_label.key == "H"
    assert by_letter.eq_subblocks == by_label.eq_subblocks
    # label match is case-insensitive
    assert model.virtual("subwoofer 1").key == "H"


def test_all_virtual_eq_names_exist_in_param_map(model: ChannelModel):
    for v in model.virtuals():
        for band in range(v.eq_band_count):
            ref = v.resolve_eq_band(band)
            assert all(n in model.params for n in ref.names)


def test_virtual_gain_writes_to_vcp_cell(pm: ParamMap):
    # hw-confirmed: virtual gain/mute -> VCP cell; no longer gated. Emits one 0x03 write.
    dev = _dry_device(pm)
    dev.model.virtual("A").gain(-6.0)
    (frame,) = _tx_frames(dev.link)
    vaddr = dev.model.virtual("A").resolve_gain().addr
    assert ((frame.payload[3] << 8) | frame.payload[4]) == vaddr
    assert frame.payload[5:] == encoding.gain_db_to_raw(-6.0).to_bytes(4, "big")
    # mute writes 0 to the same cell
    dev2 = _dry_device(pm)
    dev2.model.virtual("A").mute(True)
    (mframe,) = _tx_frames(dev2.link)
    assert mframe.payload[5:] == bytes(4)


def test_virtual_eq_band_emits_single_safeload_frame_at_base(pm: ParamMap):
    dev = _dry_device(pm)
    base_addr = dev.model.virtual("A").resolve_eq_band(2).base_addr

    n0 = len(dev.link.sent)
    dev.model.virtual("A").eq_band(2, f=1000, Q=1.0, gain_db=-3.0)

    assert len(dev.link.sent) - n0 == 1                       # exactly one frame
    (frame,) = _tx_frames(dev.link)
    assert frame.payload[0] == protocol.OP_DSP_WRITE          # 0x03 SafeLoad burst
    assert frame.payload[2] != 0xFF                           # firmware SafeLoad selector
    assert ((frame.payload[3] << 8) | frame.payload[4]) == base_addr   # resolved base address
    assert len(frame.payload[5:]) == 20                       # 5 biquad coeffs (5 x 4 bytes)


# --- routing matrices (TWO in the signal chain) ------------------------------
def test_two_routing_matrices_dims_and_axes(model: ChannelModel):
    i2v = model.routing.input_to_virtual
    v2o = model.routing.virtual_to_output
    assert i2v is not None and v2o is not None
    # input->virtual: 9 virtual rows x 6 input cols, orientation CONFIRMED.
    assert i2v.dims == (9, 6)
    assert (i2v.rows_kind, i2v.cols_kind) == ("virtual", "input")
    assert i2v.confirmed is True
    # column map (confirmed vs PC-Tool oracle): digital pair = cols 0/1, Main A..D = cols 2..5.
    assert i2v.sources == ["Digital In L", "Digital In R",
                           "Input A", "Input B", "Input C", "Input D"]
    # virtual->output: 9 output rows x 9 virtual cols, orientation CONFIRMED via screenshot
    # (outputs = destinations/right, virtual = sources/left; e.g. [E] Subwoofer <- Virtual H).
    assert v2o.dims == (9, 9)
    assert (v2o.rows_kind, v2o.cols_kind) == ("output", "virtual")
    assert v2o.confirmed is True


def test_input_to_virtual_cell_param_resolution(model: ChannelModel):
    i2v = model.routing.input_to_virtual
    # MAINMATRIX column map (confirmed vs PC-Tool oracle 2026-08-28): the digital pair occupies
    # cols 0/1 and Main inputs A/B/C/D occupy cols 2/3/4/5. A source NAME/letter resolves to a
    # column via the overlay `sources` order; an explicit INT selector is a raw column index.
    # Virtual A (row 0) <- Input A (col 2); accept the label, the bare letter, or the raw int.
    assert i2v.cell_param("A", "Input A") == "MOD_MAINMATRIX_ALG0_NXNMIXS3004P6ALG2VOL0002"
    assert i2v.cell_param("A", "A") == "MOD_MAINMATRIX_ALG0_NXNMIXS3004P6ALG2VOL0002"
    assert i2v.cell_param("A", 2) == "MOD_MAINMATRIX_ALG0_NXNMIXS3004P6ALG2VOL0002"
    # Input D is the last Main column (col 5).
    assert i2v.cell_param("A", "Input D") == "MOD_MAINMATRIX_ALG0_NXNMIXS3004P6ALG2VOL0005"
    # Virtual C (row 2) <- Input A (col 2) == G14 (2*6 + 2) — the oracle's input-routing-diag cell.
    assert i2v.cell_param("C", "Input A") == "MOD_MAINMATRIX_ALG0_NXNMIXS3004P6ALG2VOL0202"
    # Virtual H (row 7) <- Input A (col 2).
    assert i2v.cell_param("H", "Input A") == "MOD_MAINMATRIX_ALG0_NXNMIXS3004P6ALG2VOL0702"
    # The digital pair is cols 0/1.
    assert i2v.cell_param("A", "Digital In L") == "MOD_MAINMATRIX_ALG0_NXNMIXS3004P6ALG2VOL0000"
    assert i2v.cell_param("A", "Digital In R") == "MOD_MAINMATRIX_ALG0_NXNMIXS3004P6ALG2VOL0001"
    # every resolved name must exist in the ParamMap
    for cell in (i2v.cell_param("A", "Input A"), i2v.cell_param("H", "Input A"),
                 i2v.cell_param("A", "Digital In L")):
        assert cell in model.params


def test_virtual_to_output_cell_param_resolution(model: ChannelModel):
    v2o = model.routing.virtual_to_output
    assert v2o.cell_param("A", "A") == "MOD_NXMLINEAR1_ALG0_NXNMIXS3004P6ALG4VOL0000"
    assert v2o.cell_param("A", "A") in model.params
    # row=output, col=virtual: output H (row7) <- virtual I (col8).
    assert v2o.cell_param("H", "I") == "MOD_NXMLINEAR1_ALG0_NXNMIXS3004P6ALG4VOL0708"


# --- crossover (hardware-confirmed table: HP stages [0,1], LP [3,2]; BW/LR x 12/24) ----------
def test_crossover_stage_layout_and_slope(pm: ParamMap):
    dev = _dry_device(pm)
    o = dev.model.output("A")
    # stage assignment
    assert o.resolve_crossover("highpass", 0).base == "MOD_FILTERS__PHASE_12DB_A_ALG0_STAGE0_B2"
    assert o.resolve_crossover("highpass", 1).base == "MOD_FILTERS__PHASE_12DB_A_ALG0_STAGE1_B2"
    assert o.resolve_crossover("lowpass", 0).base == "MOD_FILTERS__PHASE_12DB_A_ALG0_STAGE3_B2"
    # 12 dB HP-only: the write must stamp EVERY stage so no prior filter can survive in an unused
    # stage (the 2026-08-28 hardware leak). HP stage0 = the filter; HP stage1 (unused stage of a
    # 12 dB/oct) and BOTH LP stages (off section) = the pass-through bypass image.
    o.crossover(hp=100, characteristic="butterworth", slope=12)
    writes = _stage_writes(dev.link)
    exp = b"".join(encoding.to_bytes_be(x) for x in encoding.biquad_rbj("highpass", 100, 0.7071, 0.0, 48000))
    byp = b"".join(encoding.to_bytes_be(x) for x in [0, 0, encoding.UNITY, 0, 0])
    assert writes[pm.addr(o.resolve_crossover("highpass", 0).base)] == exp   # active stage: filter
    assert writes[pm.addr(o.resolve_crossover("highpass", 1).base)] == byp   # unused HP stage bypassed
    assert writes[pm.addr(o.resolve_crossover("lowpass", 0).base)] == byp    # off LP section bypassed
    assert writes[pm.addr(o.resolve_crossover("lowpass", 1).base)] == byp


def test_crossover_24db_two_stages_and_lr_q(pm: ParamMap):
    # 24 dB = 2 cascaded stages with the Butterworth Q pair; LR12 = Q 0.5.
    dev = _dry_device(pm)
    o = dev.model.output("A")
    byp = b"".join(encoding.to_bytes_be(x) for x in [0, 0, encoding.UNITY, 0, 0])
    o.crossover(hp=300, characteristic="butterworth", slope=24)
    writes = _stage_writes(dev.link)
    for pos, q in enumerate((0.5412, 1.3066)):     # both HP stages active with the BW Q pair
        exp = b"".join(encoding.to_bytes_be(x) for x in encoding.biquad_rbj("highpass", 300, q, 0.0, 48000))
        assert writes[pm.addr(o.resolve_crossover("highpass", pos).base)] == exp
    assert writes[pm.addr(o.resolve_crossover("lowpass", 0).base)] == byp   # off LP section bypassed
    assert writes[pm.addr(o.resolve_crossover("lowpass", 1).base)] == byp
    # LR12 = single active stage Q 0.5; the unused HP stage1 is bypassed
    dev2 = _dry_device(pm)
    o2 = dev2.model.output("B")
    o2.crossover(hp=200, characteristic="linkwitz_riley", slope=12)
    w2 = _stage_writes(dev2.link)
    assert w2[pm.addr(o2.resolve_crossover("highpass", 0).base)] == \
        b"".join(encoding.to_bytes_be(x) for x in encoding.biquad_rbj("highpass", 200, 0.5, 0.0, 48000))
    assert w2[pm.addr(o2.resolve_crossover("highpass", 1).base)] == byp
    # unmapped characteristic/slope raises
    with pytest.raises(ChannelModelError):
        dev.model.output("A").crossover(hp=100, characteristic="bessel", slope=18)


def test_phase_write_emits_allpass_at_stage8(pm: ParamMap):
    # phase() must resolve the FILTERS stage-8 address (regression: it used EqBandRef.name, which
    # doesn't exist) and write an all-pass giving -deg at the crossover frequency.
    dev = _dry_device(pm)
    o = dev.model.output("F")
    o.phase(45, ref_hz=80.0)                                  # must not AttributeError
    writes = _stage_writes(dev.link)
    payload = writes[pm.addr(o.resolve_phase().base)]         # -> stage 8
    words = [int.from_bytes(payload[j:j + 4], "big") for j in range(0, 20, 4)]
    assert encoding.is_allpass(words)
    assert abs(encoding.allpass_phase_deg(words, 80.0) - (-45.0)) < 1.0
    # no crossover set + no ref_hz -> clear error, not a crash
    with pytest.raises(ChannelModelError):
        dev.model.output("A").phase(90)


def _read_section(oc, kind, writes, pm):
    """Decode a crossover section back from the written SafeLoad frames (the dsp_web pattern:
    read each stage's coeffs, keep the active ones as (corner, Q))."""
    from acodsp.channels import _XOVER_STAGES
    recs = []
    for pos in range(len(_XOVER_STAGES[kind])):
        payload = writes.get(pm.addr(oc.resolve_crossover(kind, pos).base))
        if not payload:
            continue
        words = [int.from_bytes(payload[j:j + 4], "big") for j in range(0, 20, 4)]
        rec = encoding.lphp_corner_from_coeffs(words)
        if rec is not None:
            recs.append(rec)
    return recs


def test_per_section_custom_and_lr_crossover_roundtrip(pm: ParamMap):
    from acodsp.channels import crossover_characteristic
    # Output F: HP self-defined (custom) Q1.5, single stage; LP OFF.
    dev = _dry_device(pm)
    of = dev.model.output("F")
    of.crossover_section("highpass", freq=100, characteristic="custom", slope=12, q=[1.5])
    of.crossover_section("lowpass", freq=None)                  # OFF
    w = _stage_writes(dev.link)
    hp = _read_section(of, "highpass", w, pm)
    lp = _read_section(of, "lowpass", w, pm)
    assert len(hp) == 1 and abs(hp[0][0] - 100.0) < 1.0        # 1 stage -> 12 dB, ~100 Hz
    assert abs(hp[0][1] - 1.5) < 0.02                          # custom Q recovered
    assert crossover_characteristic(12 * len(hp), hp[0][1]) == "custom"
    assert lp == []                                            # LP fully bypassed

    # Output A: HP = LR24 and LP = LR24 (per-section, independent calls).
    dev2 = _dry_device(pm)
    oa = dev2.model.output("A")
    oa.crossover_section("highpass", freq=80, characteristic="linkwitz_riley", slope=24)
    oa.crossover_section("lowpass", freq=2000, characteristic="linkwitz_riley", slope=24)
    w2 = _stage_writes(dev2.link)
    hp2 = _read_section(oa, "highpass", w2, pm)
    lp2 = _read_section(oa, "lowpass", w2, pm)
    assert len(hp2) == 2 and abs(hp2[0][0] - 80.0) < 1.0       # 2 stages -> 24 dB
    assert len(lp2) == 2 and abs(lp2[0][0] - 2000.0) < 1.0
    assert crossover_characteristic(12 * len(hp2), hp2[0][1]) == "linkwitz_riley"
    assert crossover_characteristic(12 * len(lp2), lp2[0][1]) == "linkwitz_riley"
    # custom needs an explicit q list
    with pytest.raises(ChannelModelError):
        dev.model.output("B").crossover_section("highpass", 100, "custom", 24)  # no q


def test_bypass_eq_band_writes_passthrough(pm: ParamMap):
    dev = _dry_device(pm)
    byp = b"".join(encoding.to_bytes_be(x) for x in [0, 0, encoding.UNITY, 0, 0])
    o = dev.model.output("A")
    o.eq_band(0, 100.0, 1.0, -4.0)         # a real band ...
    o.bypass_eq_band(0)                     # ... then bypass it
    payload = _stage_writes(dev.link)[pm.addr(o.resolve_eq_band(0).base)]
    assert payload == byp                  # pass-through image written to the band's stage
    words = [int.from_bytes(payload[j:j + 4], "big") for j in range(0, 20, 4)]
    assert encoding.eq_band_from_coeffs(words) is None   # recovery reads it as bypassed
    # also works on inputs (shared base method)
    ic = dev.model.input("A")
    ic.bypass_eq_band(2)
    assert _stage_writes(dev.link)[pm.addr(ic.resolve_eq_band(2).base)] == byp


def test_whole_eq_bypass_writes_mux_index(pm: ParamMap):
    from acodsp.channels import EQ_BYPASS_INDEX, EQ_ACTIVE_INDEX
    dev = _dry_device(pm)
    o = dev.model.output("A")
    ref = o.resolve_eq_bypass()
    assert ref.name.startswith("MOD_EQUALIZER_BYPASSA_") and ref.name.endswith("INDEX")
    # bypass -> mux index 1 (hardware-confirmed int32), to the resolved cell
    o.bypass_eq(True)
    f = _tx_frames(dev.link)[-1]
    assert (f.payload[3] << 8 | f.payload[4]) == pm.addr(ref.name)
    assert int.from_bytes(f.payload[5:9], "big") == EQ_BYPASS_INDEX == 1
    # re-enable -> index 0
    o.bypass_eq(False)
    assert int.from_bytes(_tx_frames(dev.link)[-1].payload[5:9], "big") == EQ_ACTIVE_INDEX == 0


def test_virtual_eq_bypass_maps_to_its_eq_region(pm: ParamMap):
    from acodsp.channels import EQ_BYPASS_INDEX, _VIRTUAL_EQ_BYPASS_PREFIX
    dev = _dry_device(pm)
    checked = 0
    for vc in dev.model.virtuals():
        try:
            band = vc.resolve_eq_band(0).base
        except ChannelModelError:
            with pytest.raises(ChannelModelError):        # pass-through virtual -> no EQ bypass
                vc.resolve_eq_bypass()
            continue
        ref = vc.resolve_eq_bypass()
        assert ref.name.endswith("INDEX")
        # the bypass cell must live in the SAME GLOBAL_EQ region as the virtual's EQ bands
        region_base = _VIRTUAL_EQ_BYPASS_PREFIX[vc.key].rsplit("_BYPASS", 1)[0] + "_"
        assert band.startswith(region_base), f"{vc.key}: {band} not in {region_base}"
        checked += 1
    assert checked >= 6
    # write path (Virtual A): mux index 1 to the resolved GLOBAL_EQ bypass cell
    va = dev.model.virtuals()[0]
    va.bypass_eq(True)
    f = _tx_frames(dev.link)[-1]
    assert (f.payload[3] << 8 | f.payload[4]) == pm.addr(va.resolve_eq_bypass().name)
    assert int.from_bytes(f.payload[5:9], "big") == EQ_BYPASS_INDEX


def test_routing_read_grids(pm: ParamMap):
    dev = _dry_device(pm)
    i2v = dev.model.routing.input_to_virtual.read(dev)
    assert len(i2v.as_grid()) == 9 and len(i2v.as_grid()[0]) == 6
    v2o = dev.model.routing.virtual_to_output.read(dev)
    assert len(v2o.as_grid()) == 9 and len(v2o.as_grid()[0]) == 9


def test_routing_set_writes_8_24_percent(pm: ParamMap):
    # routing encoder confirmed: cells are 8.24 linear gains (percent/100). 0 dB = 100% = unity.
    dev = _dry_device(pm)
    dev.model.routing.input_to_virtual.set("A", "Input A", 0.0)
    (frame,) = _tx_frames(dev.link)
    assert frame.payload[5:] == bytes.fromhex("01000000")   # 0 dB = 100% = unity
    dev.model.routing.virtual_to_output.set("A", "A", -6.0)
    frame2 = _tx_frames(dev.link)[0]
    assert frame2.payload[5:] == bytes.fromhex("00804DCE")  # -6 dB = 50%


def test_routing_set_emits_one_frame_at_resolved_cell(pm: ParamMap):
    dev = _dry_device(pm)
    cell = dev.model.routing.input_to_virtual.cell_param("A", "Input A")
    dev.model.routing.input_to_virtual.set("A", "Input A", 0.0, unsafe=True)
    assert len(dev.link.sent) == 1
    (frame,) = _tx_frames(dev.link)
    assert frame.payload[0] == protocol.OP_DSP_WRITE          # 0x03 firmware SafeLoad
    assert ((frame.payload[3] << 8) | frame.payload[4]) == dev.params.addr(cell)  # resolved cell addr
    assert len(frame.payload[5:]) == 4                        # one 8.24 mix-gain word


def test_routing_missing_matrices_degrade_gracefully(pm: ParamMap):
    # An overlay without routing.matrices yields an empty Routing container, not a crash.
    base = ChannelModel.load("MatchM54DSP", pm)
    overlay = copy.deepcopy(base.overlay)
    overlay["routing"] = {}
    model = ChannelModel(base.model_name, base.params, base.topology, overlay)
    assert bool(model.routing) is False
    assert model.routing.input_to_virtual is None
    assert model.routing.virtual_to_output is None


# --- overlay-absent fallback -------------------------------------------------
def test_builds_without_overlay(pm: ParamMap):
    model = ChannelModel(model_name="MatchM54DSP", param_map=pm,
                         topology=ChannelModel.load("MatchM54DSP", pm).topology, overlay=None)
    assert model.input_letters == ["A", "B", "C", "D"]        # physical layer still built
    assert model.output("A").label == "Output A"              # generic label
    # No overlay eq-block assignment -> no cascaded class map; input EQ still resolves.
    assert model.input("A").resolve_eq_band(0).base in model.params
    with pytest.raises(ChannelModelError):
        model.output("A").resolve_eq_band(0)                  # unassigned without overlay
