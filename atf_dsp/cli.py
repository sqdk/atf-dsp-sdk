"""cli.py — the `atf-dsp-sdk` command-line entry point.

Commands: list | identify | get-setup | set-setup N | read <name|addr> |
write <name|addr> <hex>. Writes support --dry-run.
"""
from __future__ import annotations

import argparse
import sys
from typing import Optional

from atf_dsp.device import Device
from atf_dsp.models import PID_MODELS, model_for_pid
from atf_dsp.transport import ATF_VID, find_atf_ports


def _hexs(b: bytes) -> str:
    return " ".join(f"{x:02X}" for x in b)


def _parse_hex(s: str) -> bytes:
    s = s.replace("0x", "").replace(",", " ")
    toks = s.split()
    if len(toks) == 1 and len(toks[0]) % 2 == 0:
        return bytes.fromhex(toks[0])
    return bytes(int(t, 16) for t in toks)


def cmd_list(args: argparse.Namespace) -> int:
    try:
        from serial.tools import list_ports
    except ImportError:
        print("pyserial not installed: pip install pyserial", file=sys.stderr)
        return 2
    ports = list(list_ports.comports())
    if not ports:
        print("No serial ports found.")
        return 0
    for p in ports:
        vid = getattr(p, "vid", None)
        pid = getattr(p, "pid", None)
        tag = ""
        if vid == ATF_VID:
            tag = f"  <== ATF {PID_MODELS.get(pid, f'PID 0x{pid:04X}' if pid else '?')}"
        vidpid = f"{vid:04X}:{pid:04X}" if vid and pid else "----:----"
        print(f"  {p.device:<28} [{vidpid}] {p.description or ''}{tag}")
    return 0


def cmd_identify(args: argparse.Namespace) -> int:
    with Device.connect(port=args.port, verbose=args.verbose) as dev:
        ident = dev.identify()
        setup = dev.get_setup()
        print(f"model   : {dev.model_name or ident.model or '?'}")
        print(f"fw      : {ident.firmware or '?'}")
        print(f"setup   : {setup if setup is not None else '?'}")
        print(f"raw     : {_hexs(ident.raw)}")
    return 0


def cmd_get_setup(args: argparse.Namespace) -> int:
    with Device.connect(port=args.port, verbose=args.verbose) as dev:
        setup = dev.get_setup()
        print(f"current setup: {setup if setup is not None else '?'}")
    return 0


def cmd_set_setup(args: argparse.Namespace) -> int:
    with Device.connect(port=args.port, dry_run=args.dry_run, verbose=True) as dev:
        dev.set_setup(args.n)
        if args.dry_run:
            return 0
        setup = dev.get_setup()
        ok = "SUCCESS" if setup == args.n else "(verify)"
        print(f"current setup now: {setup}  {ok}")
    return 0


def cmd_read(args: argparse.Namespace) -> int:
    with Device.connect(port=args.port, model=args.model, verbose=args.verbose) as dev:
        data = dev.read_param(args.target, nbytes=args.nbytes)
        print(f"{args.target} = {_hexs(data)}")
    return 0


def cmd_write(args: argparse.Namespace) -> int:
    data = _parse_hex(args.hex)
    with Device.connect(port=args.port, dry_run=args.dry_run, model=args.model, verbose=True) as dev:
        dev.write_param(args.target, data, safeload=not args.direct)
        if not args.dry_run:
            print(f"wrote {_hexs(data)} to {args.target}")
    return 0


def _readonly_probe(dev) -> "object":
    """A no-write reachability + read/decode smoke test: identify, current setup, and each
    output's current gain (exercises the read + decode path without changing anything)."""
    from atf_dsp.validate import Report
    rep = Report(direction="Read-only probe (no writes)")
    try:
        ident = dev.identify()
        rep.add("identify", bool(ident), "response", getattr(ident, "firmware", "ok") or "ok")
    except Exception as exc:  # noqa: BLE001 - report, don't crash the probe
        rep.add("identify", False, "response", f"error: {exc}")
    try:
        rep.add("get-setup", True, "1..N", dev.get_setup())
    except Exception as exc:  # noqa: BLE001
        rep.add("get-setup", False, "1..N", f"error: {exc}")
    try:
        for letter in dev.model.output_letters:
            g = dev.model.output(letter).gain_db()
            rep.add(f"read output {letter} gain", True, "dB or muted",
                    "muted/0 dB" if g is None else round(g, 2))
    except Exception as exc:  # noqa: BLE001
        rep.add("read output gains", False, "dB", f"error: {exc}")
    return rep


def cmd_validate(args: argparse.Namespace) -> int:
    import json

    from atf_dsp import validate

    with Device.connect(port=args.port, model=args.model, verbose=args.verbose) as dev:
        if args.print_vector:
            print(validate.format_test_vector(validate.build_test_vector(dev.model)))
            return 0

        reports = []
        # Direction A — PC-Tool oracle (offline; never writes to the amp).
        if args.pct6:
            vec = validate.build_test_vector(dev.model)
            reports.append(validate.assert_matches_pct6(args.pct6, vec))

        # Direction B — SDK write -> SDK read. Opt-in only; snapshots + restores every cell.
        if args.write:
            print("\n*** WRITE TEST ***  This briefly writes a test tuning to a SCRATCH setup and "
                  "restores every\ncell afterwards, but ANY write to a DSP can be loud. TURN AMP GAIN "
                  "DOWN or DISCONNECT\nSPEAKERS first. Select a scratch setup slot (not your tune) "
                  "before running.\n")
            if not args.yes:
                if input("Type WRITE to proceed (anything else aborts): ").strip() != "WRITE":
                    print("aborted — no writes performed.")
                    return 1
            reports.append(validate.exercise_all(dev, snapshot=True))
        elif not args.pct6:
            reports.append(_readonly_probe(dev))

        envelope = validate.report_envelope(dev, reports, notes=args.notes or "")

    for rep in reports:
        print(rep.summary())
        print()
    print(f"device : {envelope['device']['model']}  fw={envelope['device']['firmware']}  "
          f"fs={envelope['device']['fs_hz']} Hz (confirmed={envelope['device']['fs_confirmed']})")
    if args.json:
        with open(args.json, "w") as fh:
            json.dump(envelope, fh, indent=2)
        print(f"\nwrote report -> {args.json}  (attach this to a hardware-validation issue)")
    else:
        print("\nre-run with --json report.json to save a submittable report; "
              "--pct6 <file> adds the PC-Tool oracle check; --write runs the (gated) write test.")
    return 0 if envelope["all_ok"] in (True, None) else 1


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="atf-dsp-sdk", description="Audiotec Fischer ACO DSP control")
    ap.add_argument("--verbose", action="store_true", help="print TX/RX frames")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("list", help="enumerate serial ports (flag ATF devices)")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("identify", help="0x08 identify: model / firmware / setup")
    p.add_argument("--port")
    p.set_defaults(func=cmd_identify)

    p = sub.add_parser("get-setup", help="read the current active setup")
    p.add_argument("--port")
    p.set_defaults(func=cmd_get_setup)

    p = sub.add_parser("set-setup", help="switch to setup N (1-based) via 0x1F")
    p.add_argument("n", type=int)
    p.add_argument("--port")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_set_setup)

    p = sub.add_parser("read", help="read raw bytes from a DSP param (name or addr)")
    p.add_argument("target")
    p.add_argument("--port")
    p.add_argument("--model", help="force model (for name lookup without detection)")
    p.add_argument("--nbytes", type=int, default=4)
    p.set_defaults(func=cmd_read)

    p = sub.add_parser("write", help="write raw hex bytes to a DSP param (name or addr)")
    p.add_argument("target")
    p.add_argument("hex")
    p.add_argument("--port")
    p.add_argument("--model", help="force model (for name lookup without detection)")
    p.add_argument("--direct", action="store_true", help="force direct write (flag 0xFF)")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_write)

    p = sub.add_parser("validate", help="run the hardware-validation harness and emit a report")
    p.add_argument("--port")
    p.add_argument("--model", help="force model (else auto-detected by USB PID)")
    p.add_argument("--pct6", help="Direction A: a PC-Tool-authored .pct6 oracle to check our decode against")
    p.add_argument("--write", action="store_true",
                   help="Direction B: write a test tuning and read it back (snapshot/restored; GATED)")
    p.add_argument("--yes", action="store_true", help="skip the write-test confirmation prompt")
    p.add_argument("--print-vector", action="store_true",
                   help="print the exact values to enter in the PC-Tool for the --pct6 leg, then exit")
    p.add_argument("--json", help="write the submittable JSON report to this path")
    p.add_argument("--notes", help="free-text notes to include in the report")
    p.set_defaults(func=cmd_validate)

    return ap


def main(argv: Optional[list] = None) -> int:
    ap = build_parser()
    args = ap.parse_args(argv)
    # propagate top-level --verbose to sub-handlers that expect it
    if not hasattr(args, "verbose"):
        args.verbose = False
    try:
        return args.func(args)
    except (RuntimeError, ValueError, KeyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
