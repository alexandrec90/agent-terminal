"""The tab's ship button: has the tab's own agent run devkit's land skill."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from agent_terminal import attention
from agent_terminal.delivery import land_prompt
from agent_terminal.pr_board import PrBoard
from agent_terminal.runner import TabRunner
from agent_terminal.store import Tab

Handler = Callable[[dict[str, Any]], Awaitable[None]]

# An Enter close behind the text is read as part of a paste, which adds a line instead.
ENTER_DELAY_SECONDS = 0.5
# Until the agent reports that it is working, a second click would ask it twice.
SETTLE_SECONDS = 2.0


class LandRequests:
    """Types the request into the agent, when it is at its prompt to take one."""

    def __init__(self, board: PrBoard, runner: TabRunner, tab: Callable[[str], Tab]) -> None:
        self._board = board
        self._runner = runner
        self._tab = tab
        self._sending: set[str] = set()

    def handlers(self) -> dict[str, Handler]:
        """The message types answered here."""
        return {"land": self._on_land}

    def _type(self, tab_id: str, text: str) -> None:
        """Anywhere but the agent's prompt, the text would answer a question or run in
        the shell. Raises `ValueError` then."""
        status = self._runner.live[tab_id].status
        if status != attention.IDLE:
            raise ValueError(f"agent is {status}, not at its prompt")
        self._runner.input(tab_id, text)

    async def _on_land(self, message: dict[str, Any]) -> None:
        tab = self._tab(message["id"])
        fields = self._board.tab_fields(tab)
        if not fields["land"] or tab.id in self._sending:
            raise ValueError("nothing to land")
        prompt = land_prompt(tab.agent, fields["land"], self._board.pr_of_tab(tab.id))
        self._sending.add(tab.id)
        try:
            self._type(tab.id, prompt)
            await asyncio.sleep(ENTER_DELAY_SECONDS)
            self._type(tab.id, "\r")
            await asyncio.sleep(SETTLE_SECONDS)
        finally:
            self._sending.discard(tab.id)
