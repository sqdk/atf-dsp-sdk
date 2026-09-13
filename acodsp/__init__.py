"""acodsp — programmatic control of Audiotec Fischer ACO-platform DSP amplifiers.

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

from acodsp.transport import Frame, Link
from acodsp.protocol import Protocol
from acodsp.params import ParamMap
from acodsp.models import model_for_pid, at01_for_model, model_info, topology_for_model
from acodsp.device import Device, ApplyError
from acodsp import encoding
from acodsp.controls import Controls, NotConfirmedError
from acodsp.channels import ChannelModel, InputChannel, OutputChannel, VirtualChannel, RoutingMatrix, Routing
from acodsp.channels import crossover_characteristic
from acodsp.rta import RTA
from acodsp.analyzer import InputAnalyzer, Spectrum, read_input
from acodsp.pct6 import (
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
from acodsp.validate import (
    TestVector,
    Tolerance,
    DEFAULT_TOL,
    Report,
    build_test_vector,
    exercise_all,
    assert_matches_pct6,
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
]
