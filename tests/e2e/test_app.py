"""The real window, through the same mouse paths as the user, with a stand-in agent.

Needs a Windows desktop, so `addopts` deselects it and `scripts/e2e.py` runs it.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest
from driver import GIT_IDENTITY, App, Window

from agent_terminal import clipboard

pytestmark = pytest.mark.local

Scenario = Callable[[App, Window], Awaitable[None]]


def launches(app: App) -> list[dict]:
    if not app.agent_log.exists():
        return []
    return [json.loads(line) for line in app.agent_log.read_text(encoding="utf-8").splitlines()]


async def wait_launches(app: App, count: int) -> None:
    """Wait for the `count`th agent to have started: a tab shows before its process does."""
    for _ in range(100):
        if len(launches(app)) >= count:
            return
        await asyncio.sleep(0.1)
    raise AssertionError(f"only {len(launches(app))} of {count} agents started")


def process_running(pid: int) -> bool:
    found = subprocess.run(  # noqa: S603 - a fixed command around a pid this test holds
        ["pwsh", "-NoProfile", "-Command", f"[bool](Get-Process -Id {pid} -ErrorAction Ignore)"],  # noqa: S607 - pwsh from PATH, as the app itself runs it
        capture_output=True,
        text=True,
        check=True,
    )
    return found.stdout.strip() == "True"


def git(folder: Path, *args: str) -> None:
    subprocess.run(  # noqa: S603 - fixed test commands
        ["git", *args],  # noqa: S607 - git from PATH
        cwd=folder,
        check=True,
        capture_output=True,
        env={**os.environ, **GIT_IDENTITY},
    )


def make_repository(folder: Path) -> None:
    folder.mkdir(parents=True)
    git(folder, "init", "--initial-branch", "main")
    git(folder, "commit", "--allow-empty", "--message", "first")


def make_projects(base: Path) -> None:
    make_repository(base / "projects" / "alpha")
    (base / "projects" / "beta").mkdir()
    # Trusted as a whole, as the user's own projects root is.
    trusted = {(base / "projects").as_posix(): {"hasTrustDialogAccepted": True}}
    (base / "claude.json").write_text(json.dumps({"projects": trusted}), encoding="utf-8")


def make_pr(number: int, head: str, **fields) -> dict:
    return {
        "number": number,
        "title": f"Change {number}",
        "url": f"https://github.com/o/r/pull/{number}",
        "state": "OPEN",
        "headRefName": head,
        "baseRefName": "main",
        "isDraft": False,
        "author": {"login": "someone"},
        "statusCheckRollup": [
            {
                "__typename": "CheckRun",
                "name": "PR Gate",
                "status": "COMPLETED",
                "conclusion": "SUCCESS",
            }
        ],
        "reviewDecision": "",
        "mergeable": "MERGEABLE",
        "mergeStateStatus": "CLEAN",
        "labels": [],
        "body": "Why this <i>change</i> exists.",
        "comments": [],
        **fields,
    }


def write_prs(app: App, *records: dict) -> None:
    app.gh_state.write_text(json.dumps(records), encoding="utf-8")


def gh_calls(app: App) -> list[str]:
    return app.gh_log.read_text(encoding="utf-8").splitlines() if app.gh_log.exists() else []


def scenario(tmp_path: Path, body: Scenario) -> None:
    async def run() -> None:
        make_projects(tmp_path)
        app = App(tmp_path)
        try:
            await body(app, await app.start())
        finally:
            await app.dispose()
            app.kill()

    asyncio.run(run())


async def open_tab(window: Window, project: str, agent: str = "claude") -> str:
    """Open a tab from the dialog and wait for its agent. Returns the tab's id."""
    before = {tab["id"] for tab in await window.tabs()}
    await window.new_tab(project, agent=agent)
    await window.wait(f"window.__agentTerminal.tabs().length === {len(before) + 1}", "the new tab")
    tab_id = next(tab["id"] for tab in await window.tabs() if tab["id"] not in before)
    await window.wait_class(tab_id, lambda c: "idle" in c.split(), "ready")
    return tab_id


def has(name: str) -> Callable[[str], bool]:
    return lambda classes: name in classes.split()


def lacks(name: str) -> Callable[[str], bool]:
    return lambda classes: name not in classes.split()


def test_a_tab_shows_when_its_agent_needs_the_user(tmp_path):
    async def body(_app: App, window: Window) -> None:
        waiting = await open_tab(window, "alpha")
        await open_tab(window, "alpha")  # `waiting` is now off screen, so it may blink

        style = f"getComputedStyle(document.querySelector('[data-id=\"{waiting}\"]'))"

        await window.type_into(waiting, "ask\r")
        await window.wait_class(waiting, has("input"), "flagged as needing input")
        assert await window.js(f"{style}.animationName") == "blink"
        asked_color = await window.js(f"{style}.color")

        await window.type_into(waiting, "work\r")
        await window.wait_class(waiting, lacks("attention"), "cleared by the agent resuming")

        await window.type_into(waiting, "finish\r")
        await window.wait_class(waiting, has("finished"), "flagged by finishing a turn")
        assert await window.js(f"{style}.animationName") == "none", "a finished tab must not blink"
        assert await window.js(f"{style}.color") != asked_color, "the two must look different"

    scenario(tmp_path, body)


def test_right_click_greys_a_tab_until_the_agent_stops_again(tmp_path):
    async def body(_app: App, window: Window) -> None:
        tab = await open_tab(window, "alpha")
        await window.type_into(tab, "ask\r")
        await window.wait_class(tab, has("attention"), "flagged")

        await window.right_click(f'[data-id="{tab}"]')
        classes = await window.wait_class(tab, has("parked"), "greyed")
        assert "attention" not in classes.split()

        await window.right_click(f'[data-id="{tab}"]')
        await window.wait_class(tab, has("input"), "flagged again on undo")

        await window.right_click(f'[data-id="{tab}"]')
        await window.wait_class(tab, has("parked"), "greyed again")
        await window.type_into(tab, "finish\r")
        await window.wait_class(tab, has("attention"), "back on a new stop")

    scenario(tmp_path, body)


def test_groups_follow_their_sessions(tmp_path):
    async def body(_app: App, window: Window) -> None:
        first = await open_tab(window, "alpha")
        await open_tab(window, "beta")
        groups = "[...document.querySelectorAll('.group')].map(g => g.dataset.project)"
        assert await window.js(groups) == ["alpha", "beta"]

        # A group's "+" fixes the project and leaves the rest to choose.
        await window.click('[data-project="alpha"] .group-add')
        assert await window.js("document.getElementById('project-field').hidden") is True
        await window.dialog_ready()
        assert await window.js("document.getElementById('worktree').disabled") is False
        await window.start_session("codex")
        count = "document.querySelectorAll('[data-project=\"alpha\"] .tab').length"
        await window.wait(f"{count} === 2", "a second tab in the group")

        await window.click('[data-project="beta"] .tab-close')
        await window.wait(f"{groups}.length === 1", "the empty group to go")
        assert first in {tab["id"] for tab in await window.tabs()}

    scenario(tmp_path, body)


def test_a_folder_outside_git_says_why_and_can_be_made_ready(tmp_path):
    async def body(_app: App, window: Window) -> None:
        (tmp_path / "projects" / "beta" / "kept.txt").write_text("x", encoding="utf-8")
        await window.choose_project("beta")
        assert await window.js("document.getElementById('worktree').disabled") is True
        assert "isn't one" in await window.notes()

        await window.click("#prepare-worktree")
        await window.wait("!document.getElementById('worktree').disabled", "git to be set up")
        assert "isn't one" not in await window.notes()
        assert await window.js("document.getElementById('prepare-worktree').hidden") is True

        await window.start_session(worktree=True)
        await window.wait("window.__agentTerminal.tabs().length === 1", "the tab")
        assert (await window.tabs())[0]["worktree"] is True

    scenario(tmp_path, body)


def test_a_session_can_be_scoped_to_any_folder(tmp_path):
    async def body(app: App, window: Window) -> None:
        elsewhere = tmp_path / "projects" / "beta" / "deep" / "inside"
        elsewhere.mkdir(parents=True)
        await window.choose_path(tmp_path / "nowhere")
        await window.wait("!document.getElementById('folder-notes').hidden", "a warning")
        assert "doesn't exist" in await window.notes()
        assert await window.js("document.getElementById('new-tab-start').disabled") is True

        await window.type_path(str(elsewhere))
        await window.start_session()
        await window.wait("window.__agentTerminal.tabs().length === 1", "the tab")
        tab = (await window.tabs())[0]
        assert tab["project"] == "inside"
        await window.wait_class(tab["id"], has("idle"), "the agent to start")
        assert Path(launches(app)[-1]["cwd"]) == elsewhere

        # The group's "+" comes back to that same folder.
        await window.click(".group-add")
        assert await window.js("document.getElementById('new-tab-title').textContent") == (
            "New session in inside"
        )

    scenario(tmp_path, body)


def test_a_first_visit_asks_for_trust_then_loops_back_to_the_worktree(tmp_path):
    async def body(app: App, window: Window) -> None:
        repository = tmp_path / "untrusted"
        make_repository(repository)
        await window.choose_path(repository)
        await window.dialog_ready()
        await window.click("#worktree")
        await window.wait("document.getElementById('folder-notes').textContent", "a warning")
        assert "worktree starts right after" in await window.notes()

        # Codex asks for itself, so the promise is Claude's alone.
        await window.click("input[name=agent][value=codex]")
        await window.wait(
            "!document.getElementById('folder-notes').textContent.includes('Claude')", "it"
        )
        assert "Codex will ask" in await window.notes()
        assert "starts right after" not in await window.notes()

        await window.start_session(worktree=False)
        await window.wait("window.__agentTerminal.tabs().length === 1", "the tab")
        tab = (await window.tabs())[0]
        assert tab["worktree"] is True, "the toggle set above must have carried through"
        await window.wait_class(tab["id"], has("input"), "the trust screen to be flagged")
        await window.wait(
            f"window.__agentTerminal.screen('{tab['id']}').includes('you trust')", "the question"
        )
        assert "--worktree" not in launches(app)[-1]["argv"]

        await window.type_into(tab["id"], "yes\r")
        await wait_launches(app, 2)
        await window.wait_class(tab["id"], has("idle"), "the real start")
        await window.wait_class(tab["id"], lacks("input"), "the flag to clear")
        assert launches(app)[-1]["argv"][:2] == ["--session-id", tab["session_id"]]
        assert "--worktree" in launches(app)[-1]["argv"]
        assert [t["id"] for t in await window.tabs()] == [tab["id"]]
        screen = await window.js(f"window.__agentTerminal.screen('{tab['id']}')")
        assert "you trust" not in screen, "the old screen should have been cleared"

    scenario(tmp_path, body)


def test_a_declined_trust_screen_says_so_and_leaves_a_shell(tmp_path):
    async def body(app: App, window: Window) -> None:
        repository = tmp_path / "untrusted"
        make_repository(repository)
        await window.choose_path(repository)
        await window.dialog_ready()
        await window.click("#worktree")
        await window.click("#new-tab-start")
        await window.wait("window.__agentTerminal.tabs().length === 1", "the tab")
        tab = (await window.tabs())[0]
        await window.wait(
            f"window.__agentTerminal.screen('{tab['id']}').includes('you trust')", "the question"
        )
        await window.type_into(tab["id"], "no\r")
        await window.wait_class(tab["id"], has("shell"), "the shell")
        await window.wait(
            f"window.__agentTerminal.screen('{tab['id']}').includes(\"wasn't trusted\")",
            "the explanation",
        )
        assert len(launches(app)) == 1

    scenario(tmp_path, body)


def test_an_open_pr_can_be_reviewed_and_merged(tmp_path):
    async def body(app: App, window: Window) -> None:
        write_prs(app, make_pr(505, "worktree-a"))
        await open_tab(window, "alpha")
        row = "document.querySelector('[data-pr=\"505\"]')"
        await window.wait(f"!!{row}", "the PR in its project's group")
        assert await window.js(f"{row}.closest('.group').dataset.project") == "alpha"

        await window.js(f"{row}.click()")
        await window.wait("!!document.querySelector('.review-actions .primary')", "the review")
        text = await window.review_text()
        assert "Change 505" in text
        assert "main ← worktree-a" in text
        assert "PR Gate" in text
        # Text from GitHub stays text: this body and diff both try to be markup.
        assert "<i>change</i>" in text
        assert "<b>line</b>" in text
        assert await window.js("document.querySelector('#review i, #review b')") is None
        assert await window.js("document.querySelectorAll('.diff-lines .added').length") == 1
        assert await window.js("document.querySelectorAll('.diff-lines .removed').length") == 1

        await window.click(".review-actions .primary")
        assert await window.js("document.getElementById('confirm').open") is True
        assert gh_calls(app) == [], "merging must wait for the confirmation"
        await window.click("#confirm-ok")
        await window.wait(f"!{row}", "the row to go")
        await window.wait("!!document.querySelector('.review-state.merged')", "the merged state")
        assert gh_calls(app) == ["pr MERGED 505 --squash"]
        assert await window.js("document.querySelector('.review-actions .primary')") is None

    scenario(tmp_path, body)


def test_a_pr_can_be_closed_and_a_cancelled_confirmation_does_nothing(tmp_path):
    async def body(app: App, window: Window) -> None:
        write_prs(app, make_pr(7, "worktree-a"))
        await open_tab(window, "alpha")
        row = "document.querySelector('[data-pr=\"7\"]')"
        await window.wait(f"!!{row}", "the PR")
        await window.js(f"{row}.click()")
        await window.wait("!!document.querySelector('.review-actions .danger')", "the review")

        await window.click(".review-actions .danger")
        await window.click("#confirm-cancel")
        await asyncio.sleep(0.3)
        assert gh_calls(app) == []

        await window.click(".review-actions .danger")
        await window.click("#confirm-ok")
        await window.wait(f"!{row}", "the row to go")
        assert gh_calls(app) == ["pr CLOSED 7"]

    scenario(tmp_path, body)


def test_a_refused_merge_is_shown_and_the_pr_stays(tmp_path):
    async def body(app: App, window: Window) -> None:
        write_prs(app, make_pr(7, "worktree-a", refuseMerge="base branch policy forbids it"))
        await open_tab(window, "alpha")
        row = "document.querySelector('[data-pr=\"7\"]')"
        await window.wait(f"!!{row}", "the PR")
        await window.js(f"{row}.click()")
        await window.wait("!!document.querySelector('.review-actions .primary')", "the review")
        await window.click(".review-actions .primary")
        await window.click("#confirm-ok")
        await window.wait("!!document.querySelector('.review-error')", "the refusal")
        assert "policy forbids" in await window.review_text()
        assert await window.js(f"!!{row}") is True

    scenario(tmp_path, body)


def test_a_prompt_shows_where_it_stands_in_shipping(tmp_path):
    async def body(app: App, window: Window) -> None:
        tab = await open_tab(window, "alpha")
        alpha = tmp_path / "projects" / "alpha"
        git(alpha, "switch", "--create", "worktree-task")

        # An intent the pass has yet to reap.
        (alpha / "logs").mkdir()
        (alpha / "logs" / "ship-intent.md").write_text("Fix it\n\nBecause.\n", encoding="utf-8")
        chip = f"document.querySelector('[data-id=\"{tab}\"] .tab-chip')"
        await window.wait(f"{chip}?.textContent === 'ship queued'", "the ship chip")

        # Reaped: the PR it became sits on the tab, and not as a row of its own.
        (alpha / "logs" / "ship-intent.md").unlink()
        write_prs(app, make_pr(9, "worktree-task"))
        await window.wait(f"{chip}?.textContent?.startsWith('PR #9')", "the PR on the tab")
        assert await window.js("document.querySelector('[data-pr]')") is None

        # The chip opens the review, which goes back to the terminal.
        await window.js(f"{chip}.click()")
        await window.wait(
            "document.getElementById('review').textContent.includes('Change 9')", "it"
        )
        await window.js(
            "[...document.querySelectorAll('.review-actions button')]"
            ".find(b => b.textContent === 'Back to terminal').click()"
        )
        await window.wait("document.getElementById('review').hidden", "the terminal")

        # Merged on GitHub: the tab says so.
        write_prs(app, make_pr(9, "worktree-task", state="MERGED"))
        await window.wait(f"{chip}?.textContent === 'PR #9 merged'", "the merged chip")

    scenario(tmp_path, body)


def test_a_prs_prompt_can_be_reopened_from_its_worktree(tmp_path):
    async def body(app: App, window: Window) -> None:
        alpha = tmp_path / "projects" / "alpha"
        tree = alpha / ".claude" / "worktrees" / "old-session"
        git(alpha, "worktree", "add", str(tree), "-b", "worktree-old-session")
        encoded = "".join(c if c.isalnum() else "-" for c in str(tree))
        transcript = tmp_path / "claude-home" / "projects" / encoded / "the-session.jsonl"
        transcript.parent.mkdir(parents=True)
        transcript.write_text("{}\n", encoding="utf-8")
        write_prs(app, make_pr(11, "worktree-old-session"))
        await open_tab(window, "alpha")

        row = "document.querySelector('[data-pr=\"11\"]')"
        await window.wait(f"{row}?.textContent?.includes('prompt on disk')", "the prompt noted")
        await window.js(f"{row}.click()")
        reopen = (
            "[...document.querySelectorAll('.review-actions button')]"
            ".find(b => b.textContent === 'Open prompt')"
        )
        await window.wait(f"!!{reopen}", "the button")
        await window.js(f"{reopen}.click()")

        await window.wait("window.__agentTerminal.tabs().length === 2", "the prompt's tab")
        await window.wait("document.getElementById('review').hidden", "the terminal")
        await window.wait(f"!{row}", "the PR to move onto its tab")
        await wait_launches(app, 2)
        launch = launches(app)[-1]
        assert launch["argv"] == ["--resume", "the-session"]
        assert Path(launch["cwd"]) == tree

    scenario(tmp_path, body)


def test_closing_the_window_brings_every_tab_back(tmp_path):
    async def run() -> None:
        make_projects(tmp_path)
        app = App(tmp_path)
        try:
            window = await app.start()
            spoken = await open_tab(window, "alpha")
            parked = await open_tab(window, "beta", agent="codex")
            await window.right_click(f'[data-id="{parked}"]')
            await window.wait_class(parked, has("parked"), "greyed")
            await window.click(f'[data-id="{spoken}"]')
            await window.wait_class(spoken, has("active"), "selected")
            before = await window.tabs()
            session = next(tab for tab in before if tab["id"] == spoken)["session_id"]
            pids = [launch["pid"] for launch in launches(app)]
            transcript = tmp_path / "claude-home" / "projects" / "encoded" / f"{session}.jsonl"
            transcript.parent.mkdir(parents=True)
            transcript.write_text("{}\n", encoding="utf-8")

            await app.close_window()
            assert not any(process_running(pid) for pid in pids), "agents outlived the window"

            reopened = App(tmp_path)
            reopened.port = app.port
            window = await reopened.start()
            try:
                await window.wait("window.__agentTerminal.tabs().length === 2", "the tabs")
                after = await window.tabs()
                assert [t["id"] for t in after] == [t["id"] for t in before]
                await window.wait_class(spoken, has("active"), "the same tab selected")
                assert await window.tab_class(parked) == await window.wait_class(
                    parked, has("parked"), "still greyed"
                )
                await window.wait_class(spoken, has("idle"), "the agent back")
                resumed = launches(app)[-2:]
                assert any(launch["argv"][:1] == ["--resume"] for launch in resumed)
            finally:
                await reopened.dispose()
                reopened.kill()
        finally:
            await app.dispose()
            app.kill()

    asyncio.run(run())


def test_right_click_in_a_terminal_pastes(tmp_path):
    async def body(_app: App, window: Window) -> None:
        tab = await open_tab(window, "alpha")
        saved = clipboard.read_text()
        clipboard.write_text("héllo \U0001f600")
        assert clipboard.read_text() == "héllo \U0001f600", "the clipboard mangled text"
        clipboard.write_text("finish\r\n")
        try:
            await window.right_click(".term-host.shown .xterm")
            await window.wait_class(tab, has("attention"), "the pasted command to run")
        finally:
            clipboard.write_text(saved)

    scenario(tmp_path, body)


def test_a_second_launch_hands_over_instead_of_doubling(tmp_path):
    def launch_again(app: App) -> int:
        second = subprocess.run(  # noqa: S603 - this repo's own app
            app.command(), cwd=app.root, env=app.env(), timeout=30, check=False
        )
        return second.returncode

    async def body(app: App, _window: Window) -> None:
        assert await asyncio.to_thread(launch_again, app) == 0
        assert app.process is not None and app.process.poll() is None

    scenario(tmp_path, body)
