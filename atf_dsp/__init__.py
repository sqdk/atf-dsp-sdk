"""atf_dsp — programmatic control of Audiotec Fischer ACO-platform DSP amplifiers.

Part 1: transport (framing), low-level protocol primitives, param maps, device
facade and CLI. Part 2: value encoders (encoding), typed controls, RTA.
See ``docs/conductor-protocol.md``.

Note: glitch-free live updates go through the firmware's own SafeLoad path
(``write_param(safeload=True)`` -> 0x03 flag 0x00, data straight to the target
cell). The host NEVER builds a SafeLoad block or writes the SafeLoad window base
(0x0014) directly — doing so produced a full-scale output spike on hardware.
"""
from __future__ import annotations

__version__ = "0.1.0"

from atf_dsp.transport import Frame, Link
from atf_dsp.protocol import Protocol
from atf_dsp.params import ParamMap
from atf_dsp.models import model_for_pid, at01_for_model, model_info, topology_for_model
from atf_dsp.device import Device, ApplyError
from atf_dsp import encoding
from atf_dsp.controls import Controls, NotConfirmedError
from atf_dsp.channels import ChannelModel, InputChannel, OutputChannel, VirtualChannel, RoutingMatrix, Routing
from atf_dsp.channels import crossover_characteristic
from atf_dsp.rta import RTA
from atf_dsp.analyzer import InputAnalyzer, Spectrum, read_input
from atf_dsp.pct6 import (
    Setup,
    SetupMetadata,
    Channel,
    EqBand,
    RoutingBlock,
    apply_setup,
    ApplyResult,
    PidMismatchError,
    Pct6Error,
    Pct6PasswordError,
)
from atf_dsp.validate import (
    TestVector,
    Tolerance,
    DEFAULT_TOL,
    Report,
    build_test_vector,
    exercise_all,
    assert_matches_pct6,
    report_envelope,
    format_test_vector,
)

__all__ = [
    "__version__",
    "Frame",
    "Link",
    "Protocol",
    "ParamMap",
    "Device",
    "ApplyError",
    "model_for_pid",
    "model_info",
    "topology_for_model",
    "at01_for_model",
    "encoding",
    "Controls",
    "NotConfirmedError",
    "ChannelModel",
    "crossover_characteristic",
    "InputChannel",
    "OutputChannel",
    "VirtualChannel",
    "RoutingMatrix",
    "Routing",
    "RTA",
    "InputAnalyzer",
    "Spectrum",
    "read_input",
    "Setup",
    "SetupMetadata",
    "Channel",
    "EqBand",
    "RoutingBlock",
    "apply_setup",
    "ApplyResult",
    "PidMismatchError",
    "Pct6Error",
    "Pct6PasswordError",
    "TestVector",
    "Tolerance",
    "DEFAULT_TOL",
    "Report",
    "build_test_vector",
    "exercise_all",
    "assert_matches_pct6",
    "report_envelope",
    "format_test_vector",
]
