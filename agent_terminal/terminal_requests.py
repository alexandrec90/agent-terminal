"""What the page's terminals send their processes: keystrokes, a new size, a paste."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from agent_terminal.desktop import Desktop, Emit, Handler
from agent_terminal.runner import TabRunner
from agent_terminal.store import Tab

DEFAULT_SIZE = (32, 120)


class TerminalRequests:
    """Holds the terminals' size, which every tab shares, so a tab started later is the
    size of the window rather than of the default."""

    def __init__(
        self,
        runner: TabRunner,
        desktop: Desktop,
        loop: asyncio.AbstractEventLoop,
        emit: Emit,
        tab: Callable[[str], Tab],
    ) -> None:
        self._runner = runner
        self._desktop = desktop
        self._loop = loop
        self._emit = emit
        self._tab = tab
        self.size = DEFAULT_SIZE

    def handlers(self) -> dict[str, Handler]:
        """The message types answered here."""
        return {"input": self._on_input, "resize": self._on_resize, "paste": self._on_paste}

    def take_size(self, message: dict[str, Any]) -> None:
        """Keep the size a message reports. Raises `ValueError` or `KeyError` if it has
        none."""
        self.size = (int(message["rows"]), int(message["cols"]))

    async def _on_input(self, message: dict[str, Any]) -> None:
        self._runner.input(message["id"], message["data"])

    async def _on_resize(self, message: dict[str, Any]) -> None:
        self.take_size(message)
        for live in self._runner.live.values():
            live.size = self.size
            # A sleeping tab has no process: it takes the size when it wakes.
            if not live.tab.asleep:
                live.session.resize(*self.size)

    async def _on_paste(self, message: dict[str, Any]) -> None:
        tab_id = self._tab(message["id"]).id
        text = await self._loop.run_in_executor(None, self._desktop.read_clipboard)
        # Back through the page: only xterm knows if the agent wants bracketed paste.
        self._emit({"type": "paste", "id": tab_id, "text": text})
