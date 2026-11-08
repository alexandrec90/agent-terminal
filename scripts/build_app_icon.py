"""Draw the app mark, Prompt Lamp, as `app.svg` and `app.ico` from one definition.

A `>` prompt with the sidebar's amber status dot where the cursor would sit.

Windows wants an `.ico` for a window, and every SVG rasterizer for Python needs a Cairo
DLL or a new dependency, so the mark is a few shapes small enough to draw here. Both
files come out of `LAYERS`, so they cannot disagree: change the mark in this file and run
it, and `tests/test_build_app_icon.py` fails if the committed files are left behind.

Usage:
    python scripts/build_app_icon.py
"""

from __future__ import annotations

import math
import struct
import zlib
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

ICON_DIR = Path(__file__).resolve().parent.parent / "agent_terminal" / "web" / "icons"

GRID = 64
TILE = "#1a1c22"
TILE_EDGE = "#3a3d47"
INK = "#e4e5e9"
AMBER = "#ffb224"

TILE_RADIUS = 13.0
EDGE_WIDTH = 1.5
LAMP = (45.0, 42.0, 9.0)
# The prompt's outline: a 9-wide stroke with a mitred point, spelled as a polygon so the
# SVG and the rasterizer trace the same edge.
PROMPT = (
    (15.18, 11.82),
    (35.36, 32.0),
    (15.18, 52.18),
    (8.82, 45.82),
    (22.64, 32.0),
    (8.82, 18.18),
)

ICO_SIZES = (16, 20, 24, 32, 40, 48, 64, 256)
MAX_ICO_SIZE = 256
# Only the largest frame is PNG. `System.Drawing.Icon`, which pywebview loads this file
# with, is the reader to satisfy, and bitmap frames are what it has always understood.
PNG_FRAME_SIZE = 256

Distance = Callable[[float, float], float]
Point = tuple[float, float]


def rounded_box(inset: float, radius: float) -> Distance:
    """Signed distance to a rounded square filling the grid, less `inset` on each side."""
    half = GRID / 2 - inset
    middle = GRID / 2

    def distance(x: float, y: float) -> float:
        qx = abs(x - middle) - half + radius
        qy = abs(y - middle) - half + radius
        return math.hypot(max(qx, 0.0), max(qy, 0.0)) + min(max(qx, qy), 0.0) - radius

    return distance


def outline(shape: Distance, width: float) -> Distance:
    """Signed distance to a band `width` wide centered on `shape`'s edge."""
    return lambda x, y: abs(shape(x, y)) - width / 2


def disc(cx: float, cy: float, radius: float) -> Distance:
    """Signed distance to a circle."""
    return lambda x, y: math.hypot(x - cx, y - cy) - radius


def _to_segment(x: float, y: float, start: Point, end: Point) -> float:
    """Squared distance from a point to a line segment."""
    ex, ey = end[0] - start[0], end[1] - start[1]
    wx, wy = x - start[0], y - start[1]
    along = max(0.0, min(1.0, (wx * ex + wy * ey) / (ex * ex + ey * ey)))
    return (wx - ex * along) ** 2 + (wy - ey * along) ** 2


def _crosses(x: float, y: float, start: Point, end: Point) -> bool:
    """Whether a ray going right from a point crosses this edge."""
    if (start[1] > y) == (end[1] > y):
        return False
    return x < start[0] + (y - start[1]) * (end[0] - start[0]) / (end[1] - start[1])


def polygon(points: Sequence[Point]) -> Distance:
    """Signed distance to a closed polygon."""
    edges = list(zip(points, [*points[1:], points[0]], strict=True))

    def distance(x: float, y: float) -> float:
        nearest = min(_to_segment(x, y, start, end) for start, end in edges)
        inside = sum(_crosses(x, y, start, end) for start, end in edges) % 2 == 1
        return -math.sqrt(nearest) if inside else math.sqrt(nearest)

    return distance


@dataclass(frozen=True)
class Layer:
    """One shape in the mark, and the color it paints."""

    color: str
    distance: Distance


LAYERS = (
    Layer(TILE, rounded_box(0.0, TILE_RADIUS)),
    Layer(
        TILE_EDGE, outline(rounded_box(EDGE_WIDTH / 2, TILE_RADIUS - EDGE_WIDTH / 2), EDGE_WIDTH)
    ),
    Layer(INK, polygon(PROMPT)),
    Layer(AMBER, disc(*LAMP)),
)


def rgb(color: str) -> tuple[int, int, int]:
    """The channels of a `#rrggbb` color."""
    return int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16)


def _pixel(gx: float, gy: float, scale: float, layers: Sequence[Layer]) -> bytes:
    """One pixel's RGBA: each layer over the last, its edge softened across a pixel."""
    red = green = blue = alpha = 0.0
    for layer in layers:
        cover = max(0.0, min(1.0, 0.5 - layer.distance(gx, gy) * scale))
        if not cover:
            continue
        under = alpha * (1 - cover)
        alpha = cover + under
        r, g, b = rgb(layer.color)
        red = (r * cover + red * under) / alpha
        green = (g * cover + green * under) / alpha
        blue = (b * cover + blue * under) / alpha
    return bytes((round(red), round(green), round(blue), round(alpha * 255)))


def rasterize(size: int, layers: Sequence[Layer] = LAYERS) -> bytes:
    """The mark as top-down RGBA rows, `size` pixels square."""
    if not 1 <= size <= MAX_ICO_SIZE:
        raise ValueError(f"an icon frame is 1 to {MAX_ICO_SIZE} pixels, not {size}")
    scale = size / GRID
    return b"".join(
        _pixel((col + 0.5) / scale, (row + 0.5) / scale, scale, layers)
        for row in range(size)
        for col in range(size)
    )


def _png_chunk(tag: bytes, data: bytes) -> bytes:
    body = tag + data
    return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))


def encode_png(size: int, pixels: bytes) -> bytes:
    """A PNG of top-down RGBA rows."""
    stride = size * 4
    rows = b"".join(
        b"\x00" + pixels[start : start + stride] for start in range(0, len(pixels), stride)
    )
    header = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", header)
        + _png_chunk(b"IDAT", zlib.compress(rows, 9))
        + _png_chunk(b"IEND", b"")
    )


def encode_dib(size: int, pixels: bytes) -> bytes:
    """An ICO bitmap frame: header, bottom-up BGRA rows, then an AND mask.

    The header's height counts both the color rows and the mask's. The mask is all zero
    because the alpha channel decides, but readers still expect it to be there.
    """
    stride = size * 4
    header = struct.pack("<IiiHHIIiiII", 40, size, size * 2, 1, 32, 0, 0, 0, 0, 0, 0)
    color = bytearray()
    for start in range(stride * (size - 1), -1, -stride):
        row = pixels[start : start + stride]
        for offset in range(0, stride, 4):
            r, g, b, a = row[offset : offset + 4]
            color += bytes((b, g, r, a))
    mask_stride = ((size + 31) // 32) * 4
    return header + bytes(color) + bytes(mask_stride * size)


def encode_ico(frames: dict[int, bytes]) -> bytes:
    """An `.ico` holding `{size: encoded frame}`, smallest first."""
    if not frames:
        raise ValueError("an .ico needs at least one frame")
    ordered = sorted(frames.items())
    directory = struct.pack("<HHH", 0, 1, len(ordered))
    offset = len(directory) + 16 * len(ordered)
    entries = b""
    for size, data in ordered:
        side = 0 if size == MAX_ICO_SIZE else size  # one byte, so 0 stands for 256
        entries += struct.pack("<BBBBHHII", side, side, 0, 0, 1, 32, len(data), offset)
        offset += len(data)
    return directory + entries + b"".join(data for _, data in ordered)


def build_ico(sizes: Sequence[int] = ICO_SIZES) -> bytes:
    """The app's `.ico`."""
    frames = {}
    for size in sizes:
        encode = encode_png if size >= PNG_FRAME_SIZE else encode_dib
        frames[size] = encode(size, rasterize(size))
    return encode_ico(frames)


def build_svg() -> str:
    """The app's scalable icon, used as the page's favicon."""
    inset = EDGE_WIDTH / 2
    edge_side = GRID - EDGE_WIDTH
    prompt = "M" + "L".join(f"{x:g} {y:g}" for x, y in PROMPT) + "z"
    cx, cy, radius = LAMP
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {GRID} {GRID}">\n'
        "  <title>Agent Terminal</title>\n"
        f'  <rect width="{GRID}" height="{GRID}" rx="{TILE_RADIUS:g}" fill="{TILE}"/>\n'
        f'  <rect x="{inset:g}" y="{inset:g}" width="{edge_side:g}" height="{edge_side:g}" '
        f'rx="{TILE_RADIUS - inset:g}" fill="none" stroke="{TILE_EDGE}" '
        f'stroke-width="{EDGE_WIDTH:g}"/>\n'
        f'  <path d="{prompt}" fill="{INK}"/>\n'
        f'  <circle cx="{cx:g}" cy="{cy:g}" r="{radius:g}" fill="{AMBER}"/>\n'
        "</svg>\n"
    )


def main() -> int:
    """Write both files."""
    ICON_DIR.mkdir(parents=True, exist_ok=True)
    (ICON_DIR / "app.svg").write_bytes(build_svg().encode("utf-8"))
    (ICON_DIR / "app.ico").write_bytes(build_ico())
    print(f"wrote app.svg and app.ico to {ICON_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
