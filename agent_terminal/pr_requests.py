"""What the page asks of a PR: to see it, merge it, close it, or reopen its session."""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from agent_terminal.folders import display_name
from agent_terminal.pr_board import PrBoard
from agent_terminal.prs import Gh, GhError
from agent_terminal.store import Tab

Handler = Callable[[dict[str, Any]], Awaitable[None]]


class PrRequests:
    """Answers the page, in `gh`'s own words when `gh` refuses."""

    def __init__(
        self,
        board: PrBoard,
        gh: Gh,
        loop: asyncio.AbstractEventLoop,
        emit: Callable[[dict[str, Any]], None],
    ) -> None:
        self._board = board
        self._gh = gh
        self._loop = loop
        self._emit = emit

    def handlers(self) -> dict[str, Handler]:
        """The message types answered here."""
        return {
            "pr_view": self._on_view,
            "pr_merge": self._on_merge,
            "pr_close": self._on_close,
        }

    def _target(self, message: dict[str, Any]) -> tuple[Path, int]:
        """The repository and PR a message names. Only a repository the board knows is
        accepted: this is what stands between a socket message and `gh pr merge`."""
        repository = Path(message["repo"])
        if repository not in self._board.repositories():
            raise ValueError(f"unknown repository {repository}")
        return repository, int(message["number"])

    def _fail(self, repository: Path, number: int, exc: Exception) -> None:
        self._emit(
            {"type": "pr_error", "repo": str(repository), "number": number, "message": str(exc)}
        )

    async def _send_detail(self, repository: Path, number: int) -> None:
        run = self._loop.run_in_executor
        try:
            detail = await run(None, self._gh.detail, repository, number)
        except GhError as exc:
            self._fail(repository, number, exc)
            return
        self._emit(
            {
                "type": "pr_detail",
                "repo": str(repository),
                "number": number,
                "has_prompt": self._board.prompt_of(repository, number) is not None,
                "detail": detail,
            }
        )

    async def _on_view(self, message: dict[str, Any]) -> None:
        await self._send_detail(*self._target(message))

    async def _act(self, message: dict[str, Any], action: Callable[[Path, int], None]) -> None:
        repository, number = self._target(message)
        try:
            await self._loop.run_in_executor(None, action, repository, number)
        except GhError as exc:
            self._fail(repository, number, exc)
            return
        self._board.forget(repository, number)
        await self._board.refresh(repository)
        await self._send_detail(repository, number)

    async def _on_merge(self, message: dict[str, Any]) -> None:
        await self._act(message, self._gh.merge)

    async def _on_close(self, message: dict[str, Any]) -> None:
        await self._act(message, self._gh.close)

    def tab_for_prompt(self, message: dict[str, Any]) -> Tab:
        """A new tab resuming a PR's session. Raises `ValueError` if it has none."""
        repository, number = self._target(message)
        found = self._board.prompt_of(repository, number)
        if found is None:
            raise ValueError(f"no session for PR {number}")
        in_claude_tree = found.tree.parent == repository / ".claude" / "worktrees"
        return Tab(
            id=uuid.uuid4().hex,
            project=display_name(repository),
            project_path=str(repository),
            agent=found.prompt.agent,
            created_at=time.time(),
            worktree=True,
            worktree_name=found.tree.name if in_claude_tree else None,
            session_id=found.prompt.session_id,
            cwd=str(found.tree),
            title=self._board.title_of(repository, number),
        )
