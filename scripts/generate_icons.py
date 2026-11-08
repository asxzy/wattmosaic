#!/usr/bin/env python3
"""Generate the WattMosaic app icon (128x128) and logo (256x256).

Pure standard library: a minimal PNG encoder built on ``zlib`` + ``struct``.
No external URLs, no image libraries, no binary blobs checked in by hand.

Design: a dark rounded tile with a yellow "M" waveform whose middle valley is a
lightning bolt, plus a subtle mosaic of grid cells behind it. Everything is
drawn with explicit geometry so the result is reproducible byte-for-byte.

Usage:
    python scripts/generate_icons.py            # write addon/icon.png + logo.png
    python scripts/generate_icons.py --out DIR  # write into DIR instead
"""

from __future__ import annotations

import argparse
import math
import struct
import zlib
from pathlib import Path

# --- palette (RGB) ---------------------------------------------------------
BG = (15, 23, 32)          # near-black slate
BG_EDGE = (26, 38, 50)     # subtle inner border
CELL = (30, 46, 60)        # mosaic cells
CELL_LIT = (46, 68, 84)    # lit mosaic cells
AMBER = (245, 176, 66)     # brand amber
AMBER_DEEP = (214, 138, 40)  # shading
CREAM = (255, 243, 214)    # bolt highlight
WHITE = (244, 240, 232)    # "WM" counters


def _rounded_rect_alpha(x: float, y: float, w: float, h: float, r: float) -> bool:
    """True when (x, y) is inside a rounded rectangle."""
    cx = min(max(x, r), w - r)
    cy = min(max(y, r), h - r)
    return (x - cx) ** 2 + (y - cy) ** 2 <= r * r or (
        0 <= x <= w and 0 <= y <= h and (r <= x <= w - r or r <= y <= h - r)
    )


def _distance_to_segment(
    px: float, py: float, ax: float, ay: float, bx: float, by: float
) -> float:
    dx, dy = bx - ax, by - ay
    length_sq = dx * dx + dy * dy
    if length_sq == 0:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / length_sq))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def _stroke_covers(
    px: float, py: float, points: list[tuple[float, float]], width: float
) -> bool:
    half = width / 2.0
    for (ax, ay), (bx, by) in zip(points, points[1:], strict=False):
        if _distance_to_segment(px, py, ax, ay, bx, by) <= half:
            return True
    return False


def _bolt_path(size: float) -> list[tuple[float, float]]:
    """Lightning bolt descending through the middle of the mark."""
    s = size
    return [
        (0.545 * s, 0.235 * s),
        (0.430 * s, 0.520 * s),
        (0.505 * s, 0.520 * s),
        (0.455 * s, 0.770 * s),
        (0.590 * s, 0.455 * s),
        (0.512 * s, 0.455 * s),
        (0.575 * s, 0.235 * s),
    ]


def _bolt_polygon(size: float) -> list[tuple[float, float]]:
    s = size
    # Outline (clockwise) of the bolt, distinct from the *centreline* used for
    # the "M" so the bolt visually replaces the middle valley.
    return [
        (0.560 * s, 0.210 * s),
        (0.395 * s, 0.535 * s),
        (0.487 * s, 0.535 * s),
        (0.430 * s, 0.800 * s),
        (0.625 * s, 0.435 * s),
        (0.532 * s, 0.435 * s),
        (0.615 * s, 0.210 * s),
    ]


def _point_in_polygon(px: float, py: float, poly: list[tuple[float, float]]) -> bool:
    inside = False
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        if (y1 > py) != (y2 > py):
            x_at = x1 + (py - y1) * (x2 - x1) / (y2 - y1)
            if px < x_at:
                inside = not inside
    return inside


def render(size: int) -> bytes:
    """Return raw RGB bytes (size*size*3) for the mark."""
    s = float(size)
    pixels = bytearray(size * size * 3)

    # Geometry, all relative to the tile.
    margin = 0.055 * s
    tile_w = s - 2 * margin
    tile_r = 0.20 * s
    stroke_w = 0.115 * s

    # "W" and "M" strokes. Left half of the tile is W, right half is M; the
    # bolt occupies the middle valley so the pair reads as one mark.
    w_pts = [
        (0.150 * s, 0.335 * s),
        (0.222 * s, 0.690 * s),
        (0.293 * s, 0.440 * s),
        (0.364 * s, 0.690 * s),
        (0.442 * s, 0.330 * s),
    ]
    m_pts = [
        (0.558 * s, 0.690 * s),
        (0.612 * s, 0.330 * s),
        (0.700 * s, 0.555 * s),
        (0.788 * s, 0.330 * s),
        (0.850 * s, 0.690 * s),
    ]
    bolt = _bolt_polygon(s)

    # Mosaic cells behind the letters (3x3 grid of subtle squares).
    cells: list[tuple[float, float, float, float]] = []
    grid_x0, grid_y0 = 0.115 * s, 0.115 * s
    grid_w = 0.770 * s
    gap = 0.022 * s
    cell = (grid_w - 2 * gap) / 3.0
    for row in range(3):
        for col in range(3):
            cells.append(
                (
                    grid_x0 + col * (cell + gap),
                    grid_y0 + row * (cell + gap),
                    cell,
                    cell,
                )
            )

    for y in range(size):
        py = y + 0.5
        for x in range(size):
            px = x + 0.5
            i = (y * size + x) * 3

            if not _rounded_rect_alpha(
                px - margin, py - margin, tile_w, tile_w, tile_r
            ):
                pixels[i : i + 3] = bytes((0, 0, 0))
                continue

            # Base tile with a faint vertical gradient.
            t = y / max(1.0, s - 1)
            r = int(BG[0] + (BG_EDGE[0] - BG[0]) * t)
            g = int(BG[1] + (BG_EDGE[1] - BG[1]) * t)
            b = int(BG[2] + (BG_EDGE[2] - BG[2]) * t)

            # Mosaic cells (only inside the tile and outside the letters).
            for idx, (cx, cy, cw, ch) in enumerate(cells):
                if cx <= px <= cx + cw and cy <= py <= cy + ch:
                    lit = idx in (1, 3, 5, 7)
                    colour = CELL_LIT if lit else CELL
                    r, g, b = colour
                    break

            # Letters.
            on_w = _stroke_covers(px, py, w_pts, stroke_w)
            on_m = _stroke_covers(px, py, m_pts, stroke_w)
            in_bolt = _point_in_polygon(px, py, bolt)

            if in_bolt:
                # Slight vertical shading inside the bolt.
                bt = (py / s - 0.21) / 0.59
                bt = max(0.0, min(1.0, bt))
                shade = 0.10 * math.sin(bt * math.pi)
                r = int(AMBER[0] + (CREAM[0] - AMBER[0]) * shade)
                g = int(AMBER[1] + (CREAM[1] - AMBER[1]) * shade)
                b = int(AMBER[2] + (CREAM[2] - AMBER[2]) * shade)
            elif on_w or on_m:
                # Antialias the stroke edge by 1 px for a cleaner mark.
                near_w = _stroke_covers(px, py, w_pts, stroke_w + 1.4)
                near_m = _stroke_covers(px, py, m_pts, stroke_w + 1.4)
                if not (near_w or near_m):
                    pass  # keep background
                else:
                    r, g, b = WHITE
            # Rounded-tile edge highlight.
            edge = min(
                px - margin,
                py - margin,
                margin + tile_w - px,
                margin + tile_w - py,
            )
            if 0 <= edge <= 1.2:
                r = min(255, r + 18)
                g = min(255, g + 18)
                b = min(255, b + 18)

            pixels[i] = r
            pixels[i + 1] = g
            pixels[i + 2] = b
    return bytes(pixels)


def _chunk(kind: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data))
        + kind
        + data
        + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    )


def write_png(path: Path, size: int) -> None:
    raw = render(size)
    stride = size * 3
    # PNG filter type 0 (None) per scanline.
    scanlines = b"".join(
        b"\x00" + raw[y * stride : (y + 1) * stride] for y in range(size)
    )
    ihdr = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)  # 8-bit RGB
    png = (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", ihdr)
        + _chunk(b"IDAT", zlib.compress(scanlines, 9))
        + _chunk(b"IEND", b"")
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(png)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out",
        default=str(Path(__file__).resolve().parent.parent / "addon"),
        help="output directory (default: addon/)",
    )
    args = parser.parse_args()
    out = Path(args.out)
    write_png(out / "icon.png", 128)
    write_png(out / "logo.png", 256)
    for name in ("icon.png", "logo.png"):
        path = out / name
        data = path.read_bytes()
        print(f"wrote {path} ({len(data)} bytes, {struct.unpack('>II', data[16:24])})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
