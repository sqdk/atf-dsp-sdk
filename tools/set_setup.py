#!/usr/bin/env python3
"""Switch the active sound setup on an Audiotec Fischer ACO DSP amplifier over USB."""
import argparse
import sys
import time

ATF_VID = 0x2E4F  # Audiotec Fischer (MATCH / HELIX / BRAX)


def find_port():
    from serial.tools import list_ports
    for p in list_ports.comports():
        if getattr(p, "vid", None) == ATF_VID:
            return p.device
    return None


def frame(payload):
    ln = (len(payload) + 1) & 0xFF
    return bytes([0x42, ln, (~ln) & 0xFF, 0x01]) + bytes(payload) + bytes([sum(payload) & 0xFF])


def command(ser, payload, wait=0.4):
    ser.reset_input_buffer()
    ser.write(frame(payload))
    ser.flush()
    time.sleep(wait)
    resp = ser.read(256)
    # response: 0x43 | LEN | ~LEN | 0x01 | payload[LEN] | checksum
    if len(resp) >= 6 and resp[0] == 0x43:
        return resp[4:4 + resp[1]]
    return b""


def get_setup(ser):
    for _ in range(3):  # first read after opening the port can be dropped; retry
        p = command(ser, bytes([0x28, 0x40]))
        if len(p) >= 3:
            return p[2]
    return None


def set_setup(ser, slot):
    command(ser, bytes([0x1F, 0x00, 0x00, 0x09, 0x00, slot - 1, 0x00, 0x00]), wait=2.0)


def main():
    ap = argparse.ArgumentParser(description="Switch the active DSP sound setup (1-10) over USB.")
    ap.add_argument("slot", type=int, help="setup memory number, 1-10")
    ap.add_argument("--port", help="serial port (auto-detected by USB vendor id if omitted)")
    args = ap.parse_args()

    if not 1 <= args.slot <= 10:
        sys.exit("slot must be between 1 and 10")

    try:
        import serial  # noqa: F401
    except ImportError:
        sys.exit("pyserial is required:  pip install -r requirements.txt")
    import serial

    port = args.port or find_port()
    if not port:
        sys.exit("No amplifier found. Connect it via USB, or pass --port.")

    ser = serial.Serial(port, 115200, timeout=0.3)
    time.sleep(0.3)  # let the port settle after open (DTR toggle on some platforms)
    before = get_setup(ser)
    set_setup(ser, args.slot)
    after = get_setup(ser)
    ser.close()

    print(f"{port}: setup {before} -> {after}")
    if after != args.slot:
        sys.exit(f"switch failed (setup is {after}, expected {args.slot})")
    print(f"OK - now on setup {args.slot}")


if __name__ == "__main__":
    main()
