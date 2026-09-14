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
