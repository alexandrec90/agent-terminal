"""Read an SVG in a test, without `xml`: ruff's S314 refuses it, and these are our own files.

`HTMLParser` lowercases names, so `viewBox` is read as `viewbox`. It does not check
nesting, so the tree does: a stray or missing end tag raises rather than parsing quietly.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from html.parser import HTMLParser


@dataclass
class Element:
    """One tag: its attributes, the tags inside it, and its text."""

    tag: str
    attributes: dict[str, str | None] = field(default_factory=dict)
    children: list[Element] = field(default_factory=list)
    text: str = ""

    def get(self, name: str, default: str = "") -> str:
        return self.attributes.get(name) or default

    def iter(self, tag: str | None = None) -> Iterator[Element]:
        """This element and everything inside it, optionally only of one tag."""
        if tag is None or self.tag == tag:
            yield self
        for child in self.children:
            yield from child.iter(tag)

    def find(self, tag: str) -> Element:
        return next(self.iter(tag))


class _Tree(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.root = Element("document")
        self.open = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        element = Element(tag, dict(attrs))
        self.open[-1].children.append(element)
        self.open.append(element)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.open[-1].children.append(Element(tag, dict(attrs)))

    def handle_endtag(self, tag: str) -> None:
        if self.open[-1].tag != tag:
            raise ValueError(f"</{tag}> closes <{self.open[-1].tag}>")
        self.open.pop()

    def handle_data(self, data: str) -> None:
        self.open[-1].text += data


def parse(text: str) -> Element:
    """The outermost element of an SVG document."""
    tree = _Tree()
    tree.feed(text)
    tree.close()
    if len(tree.open) != 1:
        raise ValueError(f"<{tree.open[-1].tag}> is never closed")
    return tree.root.children[0]
