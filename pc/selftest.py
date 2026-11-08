"""Loopback smoke test for the video channel (no Android device required).

    python selftest.py [host] [port]

Verifies that host.py + FFmpeg produce valid length-prefixed H.264 Annex-B
NAL units, including SPS(7), PPS(8) and an IDR(5)/non-IDR(1) slice.
"""
from __future__ import annotations

import socket
import struct
import sys
import time

HOST = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1"
PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 19010


def main() -> int:
    sock = None
    deadline = time.time() + 20
    while time.time() < deadline:
        try:
            sock = socket.create_connection((HOST, PORT), timeout=2)
            break
        except OSError:
            time.sleep(0.5)
    if sock is None:
        print(f"FAIL: cannot connect to {HOST}:{PORT}")
        return 1
    sock.settimeout(10)

    counts: dict[int, int] = {}
    total = 0
    frames = 0
    try:
        for _ in range(160):
            header = b""
            while len(header) < 4:
                chunk = sock.recv(4 - len(header))
                if not chunk:
                    raise EOFError("closed while reading length")
                header += chunk
            length = struct.unpack(">I", header)[0]
            if length <= 0 or length > 4 * 1024 * 1024:
                raise ValueError(f"implausible NAL length {length}")
            nal = b""
            while len(nal) < length:
                chunk = sock.recv(length - len(nal))
                if not chunk:
                    raise EOFError("closed mid-NAL")
                nal += chunk
            kind = nal[3] & 0x1F
            counts[kind] = counts.get(kind, 0) + 1
            total += length
            frames += 1
            if 7 in counts and 8 in counts and (5 in counts or 1 in counts) and frames >= 30:
                break
    except Exception as exc:
        print(f"stream ended: {exc}")
    finally:
        sock.close()

    print(f"NAL units = {frames}, bytes = {total:,}")
    print(f"types = {counts}   (7=SPS 8=PPS 5=IDR 1=non-IDR 6=SEI)")
    ok = 7 in counts and 8 in counts and (5 in counts or 1 in counts) and total > 1000
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
