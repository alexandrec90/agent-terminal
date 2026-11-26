"""How many prompts and PRs each day saw, for the tally above the groups and its graph.

Prompts are read from `history.jsonl`, where Claude Code records everything submitted at
its prompt, in this app or out of it. PRs are GitHub's, from a search for the user's own,
kept in the data directory so only the latest are asked for. Days are local days.

What a machine did is not counted: a prompt the fix pass dispatched, or a PR that did not
come from a session the user prompted, which is for `own_prs.py` to say.

Each is also counted for its project: a prompt's by the folder it was typed in, a PR's by
its repository's name. A worktree's prompts are its repository's, and so are a subfolder's
under the projects root, so both name the project the way the sidebar's group does.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from dataclasses import astuple, dataclass, replace
from datetime import date, datetime, timedelta
from pathlib import Path, PureWindowsPath
from typing import Any

from agent_terminal.desktop import Emit, Handler
from agent_terminal.own_prs import DISPATCHED_PREFIXES, Evidence, SessionEvidence
from agent_terminal.prs import Gh, GhError, GhMissing
from agent_terminal.settings import Settings

log = logging.getLogger(__name__)

# The history file is small and only read when it has changed, so a prompt shows at once.
PROMPT_SCAN_SECONDS = 5.0
PAGE_SIZE = 100
# GitHub's search lags a new PR by a little, so a day already fetched is asked for again.
OVERLAP = timedelta(days=1)
# Claude's own commands: nothing was asked of the agent.
COMMANDS = frozenset(
    "/rc /remote-control /resume /exit /quit /clear /login /logout /mcp /model /usage /cost "
    "/config /status /help /doctor /compact /context /permissions /rename /fast /effort "
    "/hooks /plugin /agents /memory /ide /theme /vim /export /copy /rewind /tasks".split()
)
SEARCH = (
    f"query($q:String!){{search(query:$q,type:ISSUE,first:{PAGE_SIZE}){{nodes"
    "{... on PullRequest{url createdAt headRefName}}}}"
)


# --- prompts ---


def is_prompt(text: str) -> bool:
    """Whether a line of history is the user asking an agent for something."""
    words = text.split(maxsplit=1)
    return bool(words) and words[0] not in COMMANDS and not text.startswith(DISPATCHED_PREFIXES)


def count_prompts(lines: Iterable[str]) -> Counter[date]:
    """Prompts per local day. A line that is not a history record is skipped: the last
    may be half written."""
    counts: Counter[date] = Counter()
    for line in lines:
        try:
            record = json.loads(line)
            if is_prompt(record["display"]):
                counts[datetime.fromtimestamp(record["timestamp"] / 1000).date()] += 1
        except (ValueError, KeyError, TypeError, AttributeError, OverflowError, OSError):
            continue
    return counts


def project_of(folder: str, root: str) -> str:
    """The project a folder belongs to, lower-cased so a repository's name matches it: a
    worktree's repository, else the folder under `root` it is in, else the folder itself."""
    parts = [part.lower() for part in PureWindowsPath(folder).parts]
    for index in range(len(parts) - 1):
        if parts[index] == ".claude" and parts[index + 1] == "worktrees" and index:
            parts = parts[:index]
            break
    under = [part.lower() for part in PureWindowsPath(root).parts]
    if len(parts) > len(under) and parts[: len(under)] == under:
        return parts[len(under)]
    return parts[-1] if parts else ""


def count_prompts_by_project(lines: Iterable[str], root: str) -> dict[str, Counter[date]]:
    """Prompts per project and local day, skipping what `count_prompts` skips and any
    record with no folder."""
    counts: dict[str, Counter[date]] = {}
    for line in lines:
        try:
            record = json.loads(line)
            if not is_prompt(record["display"]):
                continue
            day = datetime.fromtimestamp(record["timestamp"] / 1000).date()
            project = project_of(record["project"], root)
        except (ValueError, KeyError, TypeError, AttributeError, OverflowError, OSError):
            continue
        if project:
            counts.setdefault(project, Counter())[day] += 1
    return counts


# --- pull requests ---


@dataclass(frozen=True)
class CreatedPr:
    """When a PR was opened, as GitHub states it, from which branch, and whether a session
    the user prompted is known to have done it."""

    url: str
    created_at: str
    head: str
    own: bool = False


def parse_search(text: str) -> list[CreatedPr]:
    """A search reply. Raises `GhError` on anything else."""
    try:
        nodes = json.loads(text)["data"]["search"]["nodes"]
        found = [CreatedPr(n["url"], n["createdAt"], n["headRefName"]) for n in nodes]
        for pr in found:
            datetime.fromisoformat(pr.created_at)
        return found
    except (ValueError, KeyError, TypeError) as exc:
        raise GhError(f"unexpected reply from gh: {exc}") from exc


def count_prs(prs: Iterable[CreatedPr]) -> Counter[date]:
    """The user's own PRs per local day."""
    return Counter(
        datetime.fromisoformat(pr.created_at).astimezone().date() for pr in prs if pr.own
    )


def repository_of(url: str) -> str:
    """The repository's name in a PR's URL, lower-cased: https://github.com/owner/name/pull/1."""
    parts = url.lower().split("/")
    return parts[4] if len(parts) > 4 else ""


def count_prs_by_project(prs: Iterable[CreatedPr]) -> dict[str, Counter[date]]:
    """The user's own PRs per repository and local day."""
    counts: dict[str, Counter[date]] = {}
    for pr in prs:
        project = repository_of(pr.url)
        if pr.own and project:
            day = datetime.fromisoformat(pr.created_at).astimezone().date()
            counts.setdefault(project, Counter())[day] += 1
    return counts


def mark_own(prs: Mapping[str, CreatedPr], evidence: Evidence) -> dict[str, CreatedPr]:
    """`prs` with those the evidence covers marked. A mark stays: evidence is deleted."""
    marked = {}
    for url, pr in prs.items():
        created = datetime.fromisoformat(pr.created_at).timestamp()
        own = pr.own or evidence.covers(pr.url, pr.head, created)
        marked[url] = replace(pr, own=own)
    return marked


def fetch_prs(gh: Gh, where: Path, known: Mapping[str, CreatedPr]) -> dict[str, CreatedPr]:
    """`known` and every PR of the user's since. Blocking.

    Each page starts where the last ended, by time rather than by cursor: a search never
    gives more than its first thousand results."""
    found = dict(known)
    since = ""
    if known:
        newest = max(datetime.fromisoformat(pr.created_at) for pr in known.values())
        since = (newest - OVERLAP).strftime("%Y-%m-%dT%H:%M:%SZ")
    while True:
        terms = ["is:pr author:@me sort:created-asc", f"created:>={since}" if since else ""]
        query = " ".join(term for term in terms if term)
        reply = gh.run(where, "api", "graphql", "-f", f"query={SEARCH}", "-f", f"q={query}")
        page = parse_search(reply)
        # One already known keeps its mark.
        for pr in page:
            found.setdefault(pr.url, pr)
        # A full page ending on the second it began on would be asked for again forever.
        if len(page) < PAGE_SIZE or page[-1].created_at == since:
            return found
        since = page[-1].created_at


def load_prs(path: Path) -> dict[str, CreatedPr]:
    """The PRs saved before. An unreadable file is fetched afresh: GitHub has them all."""
    try:
        prs = [CreatedPr(*entry) for entry in json.loads(path.read_text(encoding="utf-8"))]
        for pr in prs:
            datetime.fromisoformat(pr.created_at)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError, TypeError) as exc:
        log.warning("saved PRs unreadable, asking GitHub again: %s", exc)
        return {}
    return {pr.url: pr for pr in prs}


def save_prs(path: Path, prs: Mapping[str, CreatedPr]) -> None:
    """Replace the saved PRs atomically."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    rows = [astuple(pr) for pr in prs.values()]
    temporary.write_text(json.dumps(rows), encoding="utf-8")
    os.replace(temporary, path)


# --- what the page shows ---


def summarize(counts: Mapping[date, int], today: date) -> dict[str, Any]:
    """Today's count, the record, and the average of the days before today that had any:
    a day still under way would drag it down, and so would days off."""
    worked = [count for day, count in counts.items() if day < today and count > 0]
    record_day = max(counts, key=lambda day: (counts[day], -day.toordinal()), default=None)
    return {
        "today": counts.get(today, 0),
        "average": round(sum(worked) / len(worked), 1) if worked else None,
        "record": counts[record_day] if record_day else 0,
        "record_day": record_day.isoformat() if record_day else None,
    }


def describe(
    prompts: Mapping[date, int],
    prs: Mapping[date, int] | None,
    today: date,
    project_prompts: Mapping[str, Mapping[date, int]] | None = None,
    project_prs: Mapping[str, Mapping[date, int]] | None = None,
) -> dict[str, Any]:
    """The whole message. `prs` and `project_prs` are None until GitHub has answered once.
    A day before a series' first is null, not zero: nothing was being recorded. A
    project's days span the totals', null where theirs are: recording is not per project."""
    first_prompts = min(prompts, default=today)
    first_prs = min(prs, default=today) if prs is not None else today
    first = min(first_prompts, first_prs, today)

    def series(
        prompts: Mapping[date, int], prs: Mapping[date, int] | None
    ) -> dict[str, Any]:
        days = []
        for offset in range((today - first).days + 1):
            day = first + timedelta(days=offset)
            days.append(
                {
                    "day": day.isoformat(),
                    "prompts": prompts.get(day, 0) if day >= first_prompts else None,
                    "prs": prs.get(day, 0) if prs is not None and day >= first_prs else None,
                }
            )
        return {
            "prompts": summarize(prompts, today),
            "prs": summarize(prs, today) if prs is not None else None,
            "days": days,
        }

    project_prompts = project_prompts or {}
    names = sorted({*project_prompts, *(project_prs or {})})
    projects = {
        name: series(
            project_prompts.get(name, {}),
            project_prs.get(name, {}) if project_prs is not None else None,
        )
        for name in names
    }
    return {"type": "productivity", **series(prompts, prs), "projects": projects}


class Productivity:
    """Keeps the counts current once the page has asked for them."""

    def __init__(
        self,
        settings: Settings,
        emit: Emit,
        loop: asyncio.AbstractEventLoop,
        gh: Gh | None = None,
        today: Callable[[], date] = date.today,
    ) -> None:
        self._settings = settings
        self._emit = emit
        self._loop = loop
        self._gh = gh or Gh(settings)
        self._today = today
        self._stamp: tuple[int, int] | None = None
        self._prompts: Counter[date] = Counter()
        self._project_prompts: dict[str, Counter[date]] = {}
        self._prs: dict[str, CreatedPr] | None = None
        self._evidence = SessionEvidence(settings.claude_home)
        self._sent: dict[str, Any] | None = None
        self._complained = ""
        self._gh_missing = False
        self._tasks: list[asyncio.Task[None]] = []

    def handlers(self) -> dict[str, Handler]:
        """The message types answered here."""
        return {"productivity": self._on_asked}

    async def _on_asked(self, _message: dict[str, Any]) -> None:
        """Start watching, or, for a page that has reloaded, say it all again."""
        if self._tasks:
            if self._sent is not None:
                self._emit(self._sent)
            return
        self._tasks = [
            self._loop.create_task(self._watch(self.scan_prompts, PROMPT_SCAN_SECONDS)),
            self._loop.create_task(self._watch(self.poll_prs, self._settings.pr_poll_seconds)),
        ]

    def stop(self) -> None:
        for task in self._tasks:
            task.cancel()

    async def _watch(self, look: Callable[[], Any], seconds: float) -> None:
        while True:
            await look()
            await asyncio.sleep(seconds)

    async def scan_prompts(self) -> None:
        """Count the prompts again if there are more. Sends on a new day too."""
        await self._loop.run_in_executor(None, self._read_prompts)
        self._send()

    def _read_prompts(self) -> None:
        path = self._settings.claude_home / "history.jsonl"
        try:
            stat = path.stat()
            stamp = (stat.st_mtime_ns, stat.st_size)
            if stamp == self._stamp:
                return
            with path.open(encoding="utf-8", errors="replace") as handle:
                lines = handle.readlines()
            self._prompts = count_prompts(lines)
            root = str(self._settings.projects_root)
            self._project_prompts = count_prompts_by_project(lines, root)
            self._stamp = stamp
        except FileNotFoundError:
            return
        except OSError as exc:
            self._complain(f"could not read the prompt history: {exc}")

    async def poll_prs(self) -> None:
        """Ask GitHub for new PRs. A failure leaves what was known in place."""
        if self._gh_missing:
            return
        path = self._settings.created_prs_file
        if self._prs is None:
            saved = await self._loop.run_in_executor(None, load_prs, path)
            if saved:
                self._prs = saved
                self._send()
        known = self._prs or {}
        try:
            found = await self._loop.run_in_executor(None, self._find_prs, known)
            if found != known:
                await self._loop.run_in_executor(None, save_prs, path, found)
        except GhMissing as exc:
            self._gh_missing = True
            self._complain(f"no PR count without gh: {exc}")
            return
        except (GhError, OSError) as exc:
            self._complain(f"could not count PRs: {exc}")
            return
        self._prs = found
        self._send()

    def _find_prs(self, known: Mapping[str, CreatedPr]) -> dict[str, CreatedPr]:
        found = fetch_prs(self._gh, self._settings.projects_root, known)
        return mark_own(found, self._evidence.read())

    def _complain(self, message: str) -> None:
        if message != self._complained:
            self._complained = message
            log.warning("%s", message)

    def _send(self) -> None:
        """Tell the page, if anything it shows has changed."""
        prs = project_prs = None
        if self._prs is not None:
            prs = count_prs(self._prs.values())
            project_prs = count_prs_by_project(self._prs.values())
        message = describe(self._prompts, prs, self._today(), self._project_prompts, project_prs)
        if message != self._sent:
            self._sent = message
            self._emit(message)
