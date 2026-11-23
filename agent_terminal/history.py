"""Sessions a group could bring back: every one the agents recorded in the group's folder,
its repository's main checkout or any of its worktrees, and that no tab has open.

Read from the agents' own records, as `prompts.py` does for one tree, and from the files
the shipping process leaves. A worktree that has been removed is not offered: an agent
resumes a session only from the folder it ran in.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from collections.abc import Awaitable, Callable, Iterable, Iterator
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

from agent_terminal import agents, gitinfo, shipping
from agent_terminal.codex_sessions import recent_rollouts
from agent_terminal.folders import display_name, resolve_folder
from agent_terminal.pr_board import PrBoard
from agent_terminal.prompts import CODEX_LOOKBACK_SECONDS, claude_transcript_dir
from agent_terminal.prs import MERGED
from agent_terminal.settings import Settings
from agent_terminal.store import Tab

Handler = Callable[[dict[str, Any]], Awaitable[None]]

# The newest this many are offered. Each costs a read of its transcript's tail, and a
# project's main checkout can hold hundreds.
MAX_SESSIONS = 40
# Claude rewrites its title records through a session, so the last stretch has one.
TITLE_TAIL_BYTES = 256 * 1024
# The first prompt follows a few records of setup, never many.
TITLE_HEAD_LINES = 60
TITLE_LENGTH = 120


@dataclass(frozen=True)
class PastSession:
    """One session on disk, and where it ran."""

    agent: str
    session_id: str
    tree: Path
    last_active: float
    title: str = ""


# --- the folders and the sessions in them ---


def scope_trees(folder: Path) -> list[Path]:
    """`folder`, and its repository's main checkout and worktrees, each once, that exist."""
    found: dict[Path, None] = {folder: None}
    repository = gitinfo.main_checkout(folder)
    if repository is not None:
        found[repository] = None
        for tree in gitinfo.worktrees(repository).values():
            found[tree] = None
    return [tree for tree in found if tree.is_dir()]


def claude_sessions(claude_home: Path, tree: Path) -> list[PastSession]:
    """Every Claude session begun in `tree`, untitled: a title costs a read."""
    found = []
    for path in claude_transcript_dir(claude_home, tree).glob("*.jsonl"):
        try:
            stamp = path.stat().st_mtime
        except OSError:
            continue
        found.append(PastSession(agents.CLAUDE, path.stem, tree, stamp))
    return found


def codex_sessions(codex_home: Path, trees: list[Path], now: float) -> list[PastSession]:
    """Every Codex session begun in one of `trees` within the lookback."""
    wanted = set(trees)
    return [
        PastSession(agents.CODEX, rollout.session_id, Path(rollout.cwd), rollout.started_at)
        for rollout in recent_rollouts(codex_home, now - CODEX_LOOKBACK_SECONDS)
        if Path(rollout.cwd) in wanted
    ]


def _records(lines: Iterable[str]) -> Iterator[dict[str, Any]]:
    for line in lines:
        try:
            record = json.loads(line)
        except ValueError:
            continue  # the cut-off first line of a tail, or a line mid-write
        if isinstance(record, dict):
            yield record


def _prompt_text(record: dict[str, Any]) -> str:
    """What the user typed, or nothing for a record Claude Code added itself: those are
    marked `isMeta` or are wrapped in a tag, such as a slash command's echo."""
    if record.get("type") != "user" or record.get("isMeta"):
        return ""
    content = (record.get("message") or {}).get("content")
    if isinstance(content, list):
        parts = [p.get("text", "") for p in content if isinstance(p, dict)]
        content = " ".join(p for p in parts if isinstance(p, str))
    text = content.strip() if isinstance(content, str) else ""
    return "" if text.startswith("<") else text


def title_from_tail(lines: Iterable[str]) -> str:
    """A name the user gave the session, else the one Claude gave it, else its last prompt."""
    named = {"custom-title": "", "ai-title": "", "last-prompt": ""}
    keys = {"custom-title": "customTitle", "ai-title": "aiTitle", "last-prompt": "lastPrompt"}
    for record in _records(lines):
        kind = record.get("type")
        if kind in keys and isinstance(record.get(keys[kind]), str):
            named[kind] = record[keys[kind]]
    return named["custom-title"] or named["ai-title"] or named["last-prompt"]


def first_prompt(lines: Iterable[str]) -> str:
    """The first thing the user typed."""
    return next((text for record in _records(lines) if (text := _prompt_text(record))), "")


def claude_title(transcript: Path) -> str:
    """What to call a Claude session: a title record near the end, else its first prompt.
    A headless session, such as the fix pass's, writes no title."""
    try:
        with transcript.open("rb") as handle:
            size = handle.seek(0, os.SEEK_END)
            handle.seek(max(0, size - TITLE_TAIL_BYTES))
            tail = handle.read().decode("utf-8", errors="replace").splitlines()
            handle.seek(0)
            head = [handle.readline() for _ in range(TITLE_HEAD_LINES)]
    except OSError:
        return ""
    lines = (line.decode("utf-8", errors="replace") for line in head)
    title = title_from_tail(tail) or first_prompt(lines)
    return " ".join(title.split())[:TITLE_LENGTH]


def list_sessions(
    settings: Settings, folder: Path, open_ids: set[str], now: float | None = None
) -> list[PastSession]:
    """The newest sessions in `folder`'s scope that no tab has open, newest first, titled.
    Codex states no title that this app has seen a real session give, so it has none."""
    trees = scope_trees(folder)
    found = [s for tree in trees for s in claude_sessions(settings.claude_home, tree)]
    found += codex_sessions(settings.codex_home, trees, time.time() if now is None else now)
    unopened = [s for s in found if s.session_id not in open_ids]
    newest = sorted(unopened, key=lambda s: s.last_active, reverse=True)[:MAX_SESSIONS]
    return [replace(s, title=session_title(settings, s)) for s in newest]


def session_title(settings: Settings, session: PastSession) -> str:
    """What the dialog calls a session."""
    if session.agent != agents.CLAUDE:
        return ""
    directory = claude_transcript_dir(settings.claude_home, session.tree)
    return claude_title(directory / f"{session.session_id}.jsonl")


# --- where each stands in shipping ---


@dataclass(frozen=True)
class TreeFacts:
    """What a session's folder says: its branch, its repository, and its ship intent."""

    repository: Path | None
    branch: str | None
    ship: str


def read_tree_facts(tree: Path) -> TreeFacts:
    """Blocking, though only just: three small files."""
    return TreeFacts(
        gitinfo.main_checkout(tree), gitinfo.current_branch(tree), shipping.ship_stage(tree)
    )


def describe(
    session: PastSession, facts: TreeFacts, board: PrBoard, deployed: dict[str, bool]
) -> dict[str, Any]:
    """A session as the dialog shows it. `deployed` maps a merge commit to whether the main
    checkout has it."""
    pr = board.pr_for(facts.repository, facts.branch)
    shown = None
    if pr is not None:
        shown = {
            **pr.to_dict(),
            "repo": str(facts.repository),
            "deployed": deployed.get(pr.merge_commit, False),
        }
    return {
        **asdict(session),
        "tree": str(session.tree),
        "tree_name": display_name(session.tree),
        "branch": facts.branch,
        "ship": facts.ship,
        "pr": shown,
    }


def deployed_commits(board: PrBoard, facts: Iterable[TreeFacts]) -> dict[str, bool]:
    """For each merged PR among `facts`, whether its repository's main checkout contains
    the merge, which is where a project runs from once it is pulled. Blocking: git."""
    answers: dict[str, bool] = {}
    for fact in facts:
        pr = board.pr_for(fact.repository, fact.branch)
        if fact.repository and pr and pr.state == MERGED and pr.merge_commit not in answers:
            answers[pr.merge_commit] = gitinfo.contains_commit(fact.repository, pr.merge_commit)
    return answers


def tab_for(session: PastSession, folder: Path) -> Tab:
    """A tab resuming `session` in `folder`'s group."""
    repository = gitinfo.main_checkout(folder)
    in_claude_tree = (
        repository is not None and session.tree.parent == repository / ".claude" / "worktrees"
    )
    return Tab(
        id=uuid.uuid4().hex,
        project=display_name(folder),
        project_path=str(folder),
        agent=session.agent,
        created_at=time.time(),
        worktree=repository is not None and session.tree not in (repository, folder),
        worktree_name=session.tree.name if in_claude_tree else None,
        session_id=session.session_id,
        cwd=str(session.tree),
        title=session.title,
    )


# --- the page's requests ---


@dataclass(frozen=True)
class HistoryLinks:
    """How the history reaches the tabs."""

    tabs: Callable[[], list[Tab]]
    # Opens a tab, or goes to the one that already has its session.
    resume: Callable[[Tab], Awaitable[None]]


class SessionHistory:
    """Answers the dialog's asks for past sessions, and brings one back."""

    def __init__(
        self,
        settings: Settings,
        board: PrBoard,
        loop: asyncio.AbstractEventLoop,
        emit: Callable[[dict[str, Any]], None],
        links: HistoryLinks,
    ) -> None:
        self._settings = settings
        self._board = board
        self._loop = loop
        self._emit = emit
        self._links = links

    def handlers(self) -> dict[str, Handler]:
        """The message types answered here."""
        return {"past_sessions": self._on_list, "resume_session": self._on_resume}

    async def _sessions(self, folder: Path, open_ids: set[str]) -> list[PastSession]:
        return await self._loop.run_in_executor(
            None, list_sessions, self._settings, folder, open_ids
        )

    async def _on_list(self, message: dict[str, Any]) -> None:
        """Answer at once with the PRs already known, then again once GitHub has been asked
        about branches it had not been."""
        requested = message["path"]
        folder = resolve_folder(requested)
        open_ids = {tab.session_id for tab in self._links.tabs() if tab.session_id}
        sessions = await self._sessions(folder, open_ids)
        run = self._loop.run_in_executor
        facts = await run(None, lambda: [read_tree_facts(s.tree) for s in sessions])
        self._send(requested, sessions, facts, await run(None, self._deployed, facts))
        before = [self._board.pr_for(f.repository, f.branch) for f in facts]
        await self._board.look_up_branches((f.repository, f.branch) for f in facts)
        if before != [self._board.pr_for(f.repository, f.branch) for f in facts]:
            self._send(requested, sessions, facts, await run(None, self._deployed, facts))

    def _deployed(self, facts: list[TreeFacts]) -> dict[str, bool]:
        return deployed_commits(self._board, facts)

    def _send(
        self,
        requested: str,
        sessions: list[PastSession],
        facts: list[TreeFacts],
        deployed: dict[str, bool],
    ) -> None:
        rows = [describe(s, f, self._board, deployed) for s, f in zip(sessions, facts, strict=True)]
        self._emit({"type": "past_sessions", "requested": requested, "sessions": rows})

    async def _on_resume(self, message: dict[str, Any]) -> None:
        """Only a session found again on disk is resumed: the page names one, and this is
        what stands between that name and a process started in some folder. One opened
        since the list was drawn is still found, and `resume` goes to its tab."""
        folder = resolve_folder(message["path"])
        wanted = str(message["session_id"])
        found = next(
            (s for s in await self._sessions(folder, set()) if s.session_id == wanted), None
        )
        if found is None:
            raise ValueError(f"no session {wanted!r} under {folder}")
        await self._links.resume(tab_for(found, folder))
