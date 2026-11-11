"""The app mark: its drawing, its file formats, and that the committed files are current."""

from __future__ import annotations

import importlib.util
import struct
import sys
import zlib
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

import pytest
from svg_reader import parse

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "build_app_icon.py"


def load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("build_app_icon", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # A dataclass looks its module up here while the class is being built.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


icon = load()


def pixel(pixels: bytes, size: int, x: int, y: int) -> tuple[int, ...]:
    start = (y * size + x) * 4
    return tuple(pixels[start : start + 4])


def ico_frames(data: bytes) -> dict[int, bytes]:
    """`{size: frame}`, read the way an `.ico` reader walks the directory."""
    reserved, kind, count = struct.unpack_from("<HHH", data, 0)
    assert (reserved, kind) == (0, 1)
    frames = {}
    for index in range(count):
        side, _, _, _, planes, bits, length, offset = struct.unpack_from(
            "<BBBBHHII", data, 6 + 16 * index
        )
        assert (planes, bits) == (1, 32)
        frames[side or 256] = data[offset : offset + length]
    return frames


def decode_frame(size: int, frame: bytes) -> bytes:
    """A frame back to top-down RGBA rows."""
    if frame.startswith(b"\x89PNG"):
        stored = frame[frame.index(b"IDAT") + 4 : frame.index(b"IEND") - 8]
        rows = zlib.decompress(stored)
        stride = size * 4 + 1
        return b"".join(rows[start + 1 : start + stride] for start in range(0, len(rows), stride))
    stride = size * 4
    color = frame[40 : 40 + stride * size]
    pixels = bytearray()
    for start in range(stride * (size - 1), -1, -stride):
        for offset in range(start, start + stride, 4):
            blue, green, red, alpha = color[offset : offset + 4]
            pixels += bytes((red, green, blue, alpha))
    return bytes(pixels)


def same_picture(left: bytes, right: bytes) -> bool:
    """Equal, give or take the last bit of a channel a different float build could round
    the other way."""
    return len(left) == len(right) and all(
        abs(a - b) <= 1 for a, b in zip(left, right, strict=True)
    )


# ---- shapes ----


def test_disc_is_negative_inside_and_positive_outside() -> None:
    circle = icon.disc(10.0, 10.0, 5.0)
    assert circle(10.0, 10.0) == -5.0
    assert circle(15.0, 10.0) == 0.0
    assert circle(20.0, 10.0) == 5.0


def test_rounded_box_measures_from_its_rounded_corner() -> None:
    box = icon.rounded_box(0.0, 10.0)
    assert box(32.0, 32.0) < 0
    assert box(0.0, 32.0) == pytest.approx(0.0)
    # The square's own corner is cut off, by the corner radius times (sqrt 2 - 1).
    assert box(0.0, 0.0) == pytest.approx(10.0 * (2**0.5 - 1))


def test_outline_is_a_band_either_side_of_the_edge() -> None:
    band = icon.outline(icon.disc(0.0, 0.0, 10.0), 2.0)
    assert band(10.0, 0.0) == -1.0
    assert band(9.0, 0.0) == 0.0
    assert band(0.0, 0.0) == 9.0
    assert band(13.0, 0.0) == 2.0


def test_polygon_knows_the_inside_of_a_concave_outline() -> None:
    chevron = icon.polygon(icon.PROMPT)
    assert chevron(20.0, 18.0) < 0, "on the arm"
    assert chevron(14.0, 32.0) > 0, "in the notch the point wraps around"
    assert chevron(50.0, 32.0) > 0, "past the point"


def test_polygon_distance_to_an_edge_is_exact() -> None:
    square = icon.polygon([(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)])
    assert square(5.0, 5.0) == -5.0
    assert square(15.0, 5.0) == 5.0
    assert square(13.0, 14.0) == 5.0


def test_rgb_reads_each_channel() -> None:
    assert icon.rgb("#ffb224") == (255, 178, 36)


# ---- rasterizing ----


def test_rasterize_is_four_bytes_a_pixel() -> None:
    assert len(icon.rasterize(16)) == 16 * 16 * 4


def test_rasterize_leaves_the_tile_corners_clear() -> None:
    assert pixel(icon.rasterize(64), 64, 0, 0)[3] == 0


@pytest.mark.parametrize(
    ("x", "y", "color"),
    [(45, 42, icon.AMBER), (12, 16, icon.INK), (50, 12, icon.TILE)],
    ids=["lamp", "prompt", "tile"],
)
def test_rasterize_paints_each_part_of_the_mark(x: int, y: int, color: str) -> None:
    assert pixel(icon.rasterize(64), 64, x, y) == (*icon.rgb(color), 255)


def test_rasterize_softens_an_edge() -> None:
    half_covered = [icon.Layer("#ffffff", icon.disc(0.5, 2.0, 1.0))]
    # The pixel centered on the circle's edge is half covered.
    assert pixel(icon.rasterize(64, half_covered), 64, 1, 1)[3] == pytest.approx(128, abs=64)


def test_rasterize_keeps_the_color_of_a_soft_edge() -> None:
    """Straight alpha: a faint pixel is faint, not darkened toward black."""
    layers = [icon.Layer("#ffffff", icon.disc(32.0, 32.0, 20.0))]
    pixels = icon.rasterize(64, layers)
    faint = [p for p in zip(*[iter(pixels)] * 4, strict=True) if 0 < p[3] < 255]
    assert faint
    assert all(p[:3] == (255, 255, 255) for p in faint)


@pytest.mark.parametrize("size", [0, -1, 257])
def test_rasterize_refuses_a_size_an_ico_cannot_hold(size: int) -> None:
    with pytest.raises(ValueError, match="1 to 256"):
        icon.rasterize(size)


# ---- encoding ----


def test_png_round_trips_its_pixels() -> None:
    pixels = bytes(range(16))
    data = icon.encode_png(2, pixels)
    assert data.startswith(b"\x89PNG\r\n\x1a\n")
    width, height, depth, kind = struct.unpack_from(">IIBB", data, 16)
    assert (width, height, depth, kind) == (2, 2, 8, 6)

    compressed = data[data.index(b"IDAT") + 4 : data.index(b"IEND") - 8]
    rows = zlib.decompress(compressed)
    assert rows == b"\x00" + pixels[:8] + b"\x00" + pixels[8:]


def test_png_chunks_carry_a_valid_checksum() -> None:
    chunk = icon._png_chunk(b"IEND", b"")
    assert chunk == bytes.fromhex("0000000049454e44ae426082")


def test_dib_flips_rows_and_swaps_red_and_blue() -> None:
    top = bytes((1, 2, 3, 4)) * 2
    bottom = bytes((5, 6, 7, 8)) * 2
    data = icon.encode_dib(2, top + bottom)
    body = data[40:]
    assert body[:8] == bytes((7, 6, 5, 8)) * 2
    assert body[8:16] == bytes((3, 2, 1, 4)) * 2


def test_dib_header_counts_the_mask_in_its_height() -> None:
    size, width, height = struct.unpack_from("<Iii", icon.encode_dib(16, bytes(16 * 16 * 4)))
    assert (size, width, height) == (40, 16, 32)


@pytest.mark.parametrize(("size", "row"), [(16, 4), (32, 4), (33, 8), (64, 8)])
def test_dib_mask_rows_pad_to_four_bytes(size: int, row: int) -> None:
    data = icon.encode_dib(size, bytes(size * size * 4))
    assert len(data) == 40 + size * size * 4 + row * size


def test_ico_directory_points_at_each_frame() -> None:
    assert ico_frames(icon.encode_ico({32: b"bbbb", 16: b"aa"})) == {16: b"aa", 32: b"bbbb"}


def test_ico_writes_256_as_zero() -> None:
    data = icon.encode_ico({256: b"x"})
    assert data[6] == 0
    assert data[7] == 0


def test_ico_lists_frames_smallest_first() -> None:
    data = icon.encode_ico({48: b"b", 16: b"a"})
    assert data[6] == 16
    assert data[6 + 16] == 48


def test_ico_refuses_to_be_empty() -> None:
    with pytest.raises(ValueError, match="at least one frame"):
        icon.encode_ico({})


def test_build_ico_uses_bitmaps_below_the_largest_frame() -> None:
    """`System.Drawing.Icon` is the reader, and bitmap frames are the ones it can trust."""
    frames = ico_frames(icon.build_ico((16, 32)))
    assert all(not frame.startswith(b"\x89PNG") for frame in frames.values())


def test_build_ico_stores_the_largest_frame_as_png() -> None:
    frames = ico_frames(icon.build_ico((16, 256)))
    assert frames[256].startswith(b"\x89PNG")


def test_build_ico_covers_every_windows_size() -> None:
    assert set(ico_frames(icon.build_ico())) == set(icon.ICO_SIZES)


# ---- the SVG ----


def test_svg_parses_and_fills_the_grid() -> None:
    assert parse(icon.build_svg()).get("viewbox") == "0 0 64 64"


def test_svg_draws_the_lamp_and_prompt_it_rasterizes() -> None:
    root = parse(icon.build_svg())
    lamp = root.find("circle")
    prompt = root.find("path")
    assert (lamp.get("cx"), lamp.get("cy"), lamp.get("r")) == ("45", "42", "9")
    assert lamp.get("fill") == icon.AMBER
    assert prompt.get("fill") == icon.INK
    assert prompt.get("d").count("L") == len(icon.PROMPT) - 1


def test_svg_has_a_title_for_screen_readers() -> None:
    assert parse(icon.build_svg()).find("title").text == "Agent Terminal"


# ---- the committed files ----


def test_committed_svg_is_current() -> None:
    assert (icon.ICON_DIR / "app.svg").read_bytes() == icon.build_svg().encode("utf-8")


@pytest.mark.parametrize("size", icon.ICO_SIZES)
def test_committed_ico_is_current(size: int) -> None:
    """By pixel, not by byte: the 256px frame is compressed, and two Pythons' zlib builds
    compress the same pixels to different bytes."""
    frame = ico_frames((icon.ICON_DIR / "app.ico").read_bytes())[size]
    assert same_picture(decode_frame(size, frame), icon.rasterize(size))


def test_a_stale_frame_is_noticed() -> None:
    """What the check above rests on: a frame of some other mark does not pass as current."""
    other = icon.rasterize(16, [icon.Layer(icon.AMBER, icon.rounded_box(0.0, 13.0))])
    frame = icon.encode_dib(16, other)
    assert not same_picture(decode_frame(16, frame), icon.rasterize(16))


@pytest.mark.parametrize("encode", [icon.encode_dib, icon.encode_png], ids=["bitmap", "png"])
def test_decode_frame_undoes_the_encoder(encode: Callable[[int, bytes], bytes]) -> None:
    pixels = bytes(range(16))
    assert decode_frame(2, encode(2, pixels)) == pixels


def test_main_writes_both_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "icons"
    monkeypatch.setattr(icon, "ICON_DIR", target)
    assert icon.main() == 0
    assert (target / "app.svg").read_bytes() == icon.build_svg().encode("utf-8")
    assert ico_frames((target / "app.ico").read_bytes())
