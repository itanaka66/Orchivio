"""Write the Studio's PNG icons (no image library needed): a ring and a dot on the accent colour.

    python scripts/make_pwa_icons.py

``icon-192.png`` and ``icon-512.png`` fill the square; ``icon-maskable-512.png`` keeps the mark inside the central 80 % so that
platforms may crop it to a circle or squircle.
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "apps" / "web" / "public" / "icons"
BG = (47, 95, 208)  # --accent
FG = (255, 255, 255)


def png(size: int, scale: float) -> bytes:
    """A square icon; ``scale`` is the share of the width the mark may use (1.0 = edge to edge)."""
    c = (size - 1) / 2
    outer, inner, dot = size * scale * 0.34, size * scale * 0.22, size * scale * 0.09
    rows = bytearray()
    for y in range(size):
        rows.append(0)
        for x in range(size):
            # 2x2 coverage keeps the edges smooth without a rasteriser
            hit = 0
            for dy in (-0.25, 0.25):
                for dx in (-0.25, 0.25):
                    dd = ((x + dx - c) ** 2 + (y + dy - c) ** 2) ** 0.5
                    hit += (inner <= dd <= outer) or dd <= dot
            t = hit / 4
            rows += bytes(round(b + (f - b) * t) for b, f in zip(BG, FG, strict=True)) + b"\xff"

    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    header = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(bytes(rows), 9)) + chunk(b"IEND", b"")


def generate() -> dict[str, bytes]:
    return {"icon-192.png": png(192, 1.0), "icon-512.png": png(512, 1.0), "icon-maskable-512.png": png(512, 0.8)}


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    for name, data in generate().items():
        (OUT / name).write_bytes(data)
        print("wrote", OUT / name, len(data), "bytes")
