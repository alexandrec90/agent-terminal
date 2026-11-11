"""The interface icons in `web/icons/ui.svg`, and the rules that keep them sharp at 16px."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from svg_reader import Element, parse

SPRITE = Path(__file__).resolve().parent.parent / "agent_terminal" / "web" / "icons" / "ui.svg"

ICONS = {
    "working",
    "input",
    "finished",
    "parked",
    "exited",
    "pr-open",
    "pr-merged",
    "pr-closed",
    "conflict",
    "check",
    "failing",
    "pending",
    "ship",
    "add",
    "close",
    "folder",
    "terminal",
    "worktree",
}

# A move or line command: its coordinates are the corners of straight strokes.
STRAIGHT = re.compile(r"[MLHV][^MLHVACQSTZlhvacqstz]*")
NUMBER = re.compile(r"-?\d*\.?\d+")


def symbols() -> dict[str, Element]:
    root = parse(SPRITE.read_text(encoding="utf-8"))
    return {symbol.get("id"): symbol for symbol in root.iter("symbol")}


def test_reader_refuses_a_mismatched_end_tag() -> None:
    with pytest.raises(ValueError, match="closes <g>"):
        parse("<svg><g></svg>")


def test_reader_refuses_a_tag_left_open() -> None:
    with pytest.raises(ValueError, match="never closed"):
        parse("<svg><g></g>")


def test_sprite_holds_every_icon_the_app_uses() -> None:
    assert set(symbols()) == ICONS


@pytest.mark.parametrize("name", sorted(ICONS))
def test_icon_is_on_the_pixel_aligned_grid(name: str) -> None:
    assert symbols()[name].get("viewbox") == "-.5 -.5 16 16"


@pytest.mark.parametrize("name", sorted(ICONS))
def test_icon_takes_its_color_from_the_page(name: str) -> None:
    """A fixed color here would silently ignore what a tab's state asks for."""
    symbol = symbols()[name]
    painted = [
        element.get(attribute)
        for element in symbol.iter()
        for attribute in ("fill", "stroke")
        if element.get(attribute)
    ]
    assert painted
    assert set(painted) <= {"none", "currentColor"}


@pytest.mark.parametrize("name", sorted(ICONS))
def test_icon_stays_inside_its_box(name: str) -> None:
    """Stroke and all, or the far side of the icon is clipped."""
    symbol = symbols()[name]
    for element in symbol.iter("circle"):
        cx, cy, radius = (float(element.get(key)) for key in ("cx", "cy", "r"))
        reach = radius + 0.75
        assert cx - reach >= -0.5
        assert cy - reach >= -0.5
        assert cx + reach <= 15.5
        assert cy + reach <= 15.5


@pytest.mark.parametrize("name", sorted(ICONS))
def test_absolute_path_points_sit_on_whole_pixels(name: str) -> None:
    """A half coordinate puts a 1.5px stroke across two columns, which is the blur the
    grid exists to prevent. Curves and the circles are free to go where they like."""
    for path in symbols()[name].iter("path"):
        for command in STRAIGHT.findall(path.get("d")):
            for value in NUMBER.findall(command):
                assert float(value).is_integer(), f"{name}: {command}"


def test_sprite_has_no_scripts_or_external_references() -> None:
    """Served into the page's own document, so it must stay plain drawing."""
    text = SPRITE.read_text(encoding="utf-8")
    assert "<script" not in text
    assert "http://www.w3.org/2000/svg" in text
    assert text.count("http") == 1
