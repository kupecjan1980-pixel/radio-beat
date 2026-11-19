#!/usr/bin/env python3
"""Sound Blaster X3: talk to its control port (USB CDC serial, /dev/ttyACM*) from Linux.

Creative's app drives the X3 through CTCDC.dll: a text handshake ("whoareyou" → "unlock" with an AES-256-GCM
answer → "SW_MODE1"), then binary frames 5A [opcode] [length] [payload]. Worked out from the Windows app's
decompiled X3 plug-in (SXFI = button 30, its state = bit 29 of the button mask) and the published Katana V2X
protocol (same DLL family, same key bytes; https://blog.nns.ee/2026/02/20/katana-v2x-re).

   sudo python3 x3_sxfi.py            read only: firmware version, button states, output
   sudo python3 x3_sxfi.py --toggle   SXFI switched over and back (like pressing the SXFI button twice)
   sudo python3 x3_sxfi.py --on / --off

Everything sent and received is printed in hex, for checking. The unlock step needs python3-cryptography (only if
the X3's firmware asks for it; old firmware doesn't).
"""

import glob
import os
import select
import sys
import termios
import time

KEY_DATA = bytes([0xD3, 0x1A, 0x21, 0x27, 0x9B, 0xE3, 0x46, 0xF0, 0x99, 0x9D, 0x6E, 0xC4, 0xC3, 0xFE, 0xBE, 0x98,
                  0x90, 0x18, 0x69, 0xC1, 0x18, 0xFB, 0xB1, 0x25, 0x6E, 0x0C, 0xE0, 0x7B])  # (in CTCDC.dll)
CREATIVE = "041e"
SXFI_BUTTON = 30


def hx(b: bytes) -> str:
    return " ".join(f"{x:02x}" for x in b)


def find_port():
    for tty in sorted(glob.glob("/sys/class/tty/ttyACM*")):
        dev = os.path.realpath(os.path.join(tty, "device"))
        for up in (dev, os.path.dirname(dev), os.path.dirname(os.path.dirname(dev))):
            try:
                vid = open(os.path.join(up, "idVendor")).read().strip()
                pid = open(os.path.join(up, "idProduct")).read().strip()
                prod = open(os.path.join(up, "product")).read().strip() if os.path.exists(
                    os.path.join(up, "product")) else ""
            except OSError:
                continue
            if vid == CREATIVE:
                return "/dev/" + os.path.basename(tty), pid, prod
    return None, None, None


class Port:
    def __init__(self, path: str):
        self.fd = os.open(path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        attrs = termios.tcgetattr(self.fd)
        attrs[0] = 0  # iflag
        attrs[1] = 0  # oflag
        attrs[2] = termios.CS8 | termios.CREAD | termios.CLOCAL  # cflag
        attrs[3] = 0  # lflag (raw)
        attrs[4] = attrs[5] = termios.B115200
        termios.tcsetattr(self.fd, termios.TCSANOW, attrs)
        termios.tcflush(self.fd, termios.TCIOFLUSH)

    def write(self, data: bytes, what: str = "") -> None:
        print(f"  TX {what:12} {hx(data)}  {data!r}" if what else f"  TX {hx(data)}")
        os.write(self.fd, data)

    def read(self, timeout: float = 1.0) -> bytes:
        out, end = b"", time.monotonic() + timeout
        while time.monotonic() < end:
            r, _, _ = select.select([self.fd], [], [], max(0.0, end - time.monotonic()))
            if not r:
                break
            try:
                chunk = os.read(self.fd, 4096)
            except BlockingIOError:
                continue
            if not chunk:
                break
            out += chunk
            end = max(end, time.monotonic() + 0.15)  # (the rest of a burst)
        if out:
            print(f"  RX {' ' * 12} {hx(out)}  {out!r}")
        else:
            print("  RX (nothing)")
        return out


def frames(data: bytes) -> list[bytes]:
    out, i = [], 0
    while i < len(data):
        if data[i] == 0x5A and i + 2 < len(data):
            n = 3 + data[i + 2]
            out.append(data[i:i + n])
            i += n
        else:
            i += 1
    return out


def unlock(port: Port, challenge: bytes, pid_hex: str) -> bool:
    body = challenge[len(b"whoareyou"):].rstrip(b"\r\n")
    if len(body) < 36:
        print("  (challenge too short: not the Katana-style unlock)")
        return False
    header, pid, nonce = body[0:2], body[2:4], body[4:36]
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError:
        print("  !! the X3 wants the unlock answer, which needs python3-cryptography (not installed)")
        return False
    key = header + KEY_DATA + pid
    iv = os.urandom(16)
    sealed = AESGCM(key).encrypt(iv[:12], nonce, None)  # ciphertext (32) + tag (16)
    port.write(b"unlock" + iv + sealed + b"\r\n", "unlock")
    ok = b"unlock_OK" in port.read(2.0)
    print("  unlocked" if ok else "  !! no unlock_OK")
    return ok


def main() -> int:
    path, pid, prod = find_port()
    if not path:
        print("No Creative control port (/dev/ttyACM*) found. Is the X3 plugged in (USB)?")
        return 1
    print(f"Port {path}: Creative {prod or ''} (USB 041e:{pid})")
    port = Port(path)

    print("Probe (already in command mode?):")
    port.write(bytes([0x5A, 0x03, 0x00]) + b"\r\n", "ping")
    if not frames(port.read(0.8)):
        print("Handshake:")
        port.write(b"whoareyou\r\n", "whoareyou")
        resp = port.read(2.0)
        if resp.startswith(b"whoareyou") and len(resp) >= 45:
            if not unlock(port, resp, pid):
                return 2
        else:
            print("  (no challenge: old firmware, or already unlocked)")
        port.write(b"SW_MODE1\r\n", "SW_MODE1")
        port.read(0.5)
        port.write(bytes([0x5A, 0x03, 0x00]), "ping")
        port.read(0.8)

    print("Firmware version:")
    port.write(bytes([0x5A, 0x09, 0x01, 0x02]), "fw version")
    port.read(1.0)
    print("Output:")
    port.write(bytes([0x5A, 0x2C, 0x01, 0x01]), "query output")
    port.read(1.0)
    print("Buttons (SXFI = bit 29 of the mask):")
    port.write(bytes([0x5A, 0x26, 0x03, 0x08, 0xFF, 0xFF]), "query buttons")
    resp = port.read(1.0)

    want = None
    if "--on" in sys.argv:
        want = [1]
    elif "--off" in sys.argv:
        want = [0]
    elif "--toggle" in sys.argv:
        want = [1, 0]  # over and back, like two presses (the order doesn't matter for re-locking the receiver)
    for state in want or []:
        print(f"Set SXFI {'on' if state else 'off'}:")
        port.write(bytes([0x5A, 0x26, 0x05, 0x07, SXFI_BUTTON, 0x00, state, 0x00]), f"sxfi {state}")
        port.read(1.5)
        time.sleep(1.0)
    if want:
        print("Buttons now:")
        port.write(bytes([0x5A, 0x26, 0x03, 0x08, 0xFF, 0xFF]), "query buttons")
        port.read(1.0)
    return 0


if __name__ == "__main__":
    sys.exit(main())
