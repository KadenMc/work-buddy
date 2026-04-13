#!/usr/bin/env python3
"""Generate minimal placeholder icons for the Chrome extension.

Run once from the chrome_extension/ directory:
    python generate_icons.py
"""

import struct
import zlib
from pathlib import Path


def make_png(size: int, r: int, g: int, b: int) -> bytes:
    """Create a minimal solid-color RGB PNG image."""

    def chunk(ctype: bytes, data: bytes) -> bytes:
        c = ctype + data
        crc = zlib.crc32(c) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + c + struct.pack(">I", crc)

    header = b"\x89PNG\r\n\x1a\n"
    ihdr = chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))

    raw = b""
    for _ in range(size):
        raw += b"\x00" + bytes([r, g, b]) * size

    idat = chunk(b"IDAT", zlib.compress(raw))
    iend = chunk(b"IEND", b"")

    return header + ihdr + idat + iend


def main() -> None:
    here = Path(__file__).parent
    for size in (16, 48, 128):
        path = here / f"icon{size}.png"
        path.write_bytes(make_png(size, 66, 133, 244))  # Google blue
        print(f"Created {path.name}")


if __name__ == "__main__":
    main()
