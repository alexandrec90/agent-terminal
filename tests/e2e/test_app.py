"""The real window, through the same mouse paths as the user, with a stand-in agent.

Needs a Windows desktop with WebView2, so `addopts` deselects it and `scripts/e2e.py --ci`
runs it, in `PR Gate`'s `E2E` job on `windows-latest`. Nothing here reads this PC: the
projects, agents, `gh` and every agent's home are temp folders and stand-ins.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path

import pytest
from driver import GIT_IDENTITY, App, Window
from fake_agent import PROMPT

from agent_terminal import clipboard

pytestmark = pytest.mark.ci_e2e

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
    found = subprocess.run(
        ["pwsh", "-NoProfile", "-Command", f"[bool](Get-Process -Id {pid} -ErrorAction Ignore)"],  # noqa: S607 - pwsh from PATH, as the app itself runs it
        capture_output=True,
        text=True,
        check=True,
    )
    return found.stdout.strip() == "True"


def git(folder: Path, *args: str) -> None:
    subprocess.run(
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
    # The projects root's trust stops at a repository, so alpha has its own, as a
    # repository the user has run Claude in does.
    trusted = {
        (base / "projects").as_posix(): {"hasTrustDialogAccepted": True},
        (base / "projects" / "alpha").as_posix(): {"hasTrustDialogAccepted": True},
    }
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


def icon_in(selector: str) -> str:
    """A page expression naming the sprite symbol the first icon under `selector` draws."""
    return f"document.querySelector({json.dumps(selector + ' .icon')})?.dataset.icon"


def test_the_sidebar_draws_each_state_from_the_icon_sprite(tmp_path):
    async def body(_app: App, window: Window) -> None:
        tab = await open_tab(window, "alpha")
        mark = f'[data-id="{tab}"] .tab-dot'
        # Ready is one of the quiet states: the plain dot, with no icon in it.
        assert await window.js(f"document.querySelector({json.dumps(mark)}).children.length") == 0

        await window.type_into(tab, "ask\r")
        await window.wait(f"{icon_in(mark)} === 'input'", "the input icon")
        await window.type_into(tab, "work\r")
        await window.wait_class(tab, lacks("attention"), "cleared by the agent resuming")
        await window.type_into(tab, "finish\r")
        await window.wait(f"{icon_in(mark)} === 'finished'", "the finished icon")
        await window.right_click(f'[data-id="{tab}"]')
        await window.wait(f"{icon_in(mark)} === 'parked'", "the parked icon")

        assert await window.js(icon_in(f'[data-id="{tab}"] .tab-close')) == "close"
        assert await window.js(icon_in('[data-project="alpha"] .group-add')) == "add"
        assert await window.js(icon_in("#new-group")) == "add"

        # Drawn, not an empty 16px box: a `<use>` whose symbol never arrived measures zero.
        drawn = (
            "(() => { const uses = [...document.querySelectorAll('#sidebar .icon use')];"
            " return uses.length >= 4 && uses.every(u => u.getBBox().width > 0); })()"
        )
        await window.wait(drawn, "every sidebar icon to be drawn")
        size = "getComputedStyle(document.querySelector('#sidebar .icon')).width"
        assert await window.js(size) == "16px", "the sprite is sharp at 16px only"

        favicon = (
            "fetch(document.querySelector('link[rel=icon]').href)"
            ".then(r => r.ok && r.headers.get('content-type'))"
        )
        assert "svg" in await window.js(favicon)

    scenario(tmp_path, body)


def test_shift_enter_and_ctrl_enter_reach_the_agent_as_alt_enter(tmp_path):
    async def body(_app: App, window: Window) -> None:
        await open_tab(window, "alpha")
        await window.js("document.querySelector('.term-host.shown textarea').focus()")
        await window.js(
            "(() => { window.__typed = []; const send = WebSocket.prototype.send;"
            " WebSocket.prototype.send = function (data) { const sent = JSON.parse(data);"
            " if (sent.type === 'input') window.__typed.push(sent.data);"
            " return send.call(this, data); }; })()"
        )
        await window.press_enter(modifiers=8)
        await window.press_enter(modifiers=2)
        await window.press_enter()
        await window.wait("window.__typed.includes('\\r')", "the plain Enter")
        # A focus report is sent whenever the window gains or loses it, as it may mid-test.
        focus = ("\x1b[I", "\x1b[O")
        typed = [data for data in await window.js("window.__typed") if data not in focus]
        assert typed == ["\x1b\r", "\x1b\r", "\r"]

    scenario(tmp_path, body)


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

        # Looked at, a finished turn has been read: the blue goes.
        await window.js(f"document.querySelector('[data-id=\"{waiting}\"]').click()")
        await window.wait_class(waiting, lacks("attention"), "cleared by viewing it")

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
        assert await window.js("document.getElementById('worktree').checked") is True
        await window.start_session("codex")
        count = "document.querySelectorAll('[data-project=\"alpha\"] .tab').length"
        await window.wait(f"{count} === 2", "a second tab in the group")

        # Its last tab closed, a group stays, with its "+" and "x", until that "x".
        await window.click('[data-project="beta"] .tab-close')
        beta_tabs = "document.querySelectorAll('[data-project=\"beta\"] .tab').length"
        await window.wait(f"{beta_tabs} === 0", "the group's last tab to close")
        assert await window.js(groups) == ["alpha", "beta"]
        assert first in {tab["id"] for tab in await window.tabs()}
        for control in (".group-add", ".group-close"):
            selector = json.dumps(f'[data-project="beta"] {control}')
            assert await window.js(f"document.querySelector({selector}) !== null") is True

        await window.drag('[data-project="beta"] .group-head', '[data-project="alpha"]')
        await window.wait(f"{groups}[0] === 'beta'", "the empty group dragged up")

        # Nothing in it to lose, so its "x" asks nothing.
        await window.click('[data-project="beta"] .group-close')
        await window.wait(f"{groups}.length === 1", "the empty group to go")
        assert await window.js(groups) == ["alpha"]

    scenario(tmp_path, body)


def test_tabs_and_groups_are_reordered_by_dragging(tmp_path):
    async def body(_app: App, window: Window) -> None:
        first = await open_tab(window, "alpha")
        second = await open_tab(window, "alpha")
        other = await open_tab(window, "beta")
        groups = "[...document.querySelectorAll('.group')].map(g => g.dataset.project)"
        alpha_tabs = "[...document.querySelectorAll('[data-project=\"alpha\"] .tab')]"
        order = f"{alpha_tabs}.map(t => t.dataset.id)"
        assert await window.js(order) == [first, second]

        await window.drag(f'[data-id="{second}"]', f'[data-id="{first}"]')
        await window.wait(f"{order}[0] === {json.dumps(second)}", "the tab dragged up")
        await window.drag(f'[data-id="{second}"]', f'[data-id="{first}"]', below=True)
        await window.wait(f"{order}[0] === {json.dumps(first)}", "the tab dragged back down")

        # Out of its group it will not go: the drop is refused and nothing moves.
        await window.drag(f'[data-id="{first}"]', f'[data-id="{other}"]')
        await asyncio.sleep(0.5)
        assert await window.js(order) == [first, second]
        assert await window.js(
            f"{json.dumps(first)} === document.querySelector("
            "'[data-project=\"alpha\"] .tab').dataset.id"
        )

        await window.drag('[data-project="beta"] .group-head', '[data-project="alpha"]')
        await window.wait(f"{groups}[0] === 'beta'", "the group dragged up")
        assert await window.js(groups) == ["beta", "alpha"]
        assert (
            await window.js(
                "document.querySelectorAll('.dragging, .drop-before, .drop-after').length"
            )
            == 0
        )

    scenario(tmp_path, body)


def test_a_group_folds_to_its_heading_and_back(tmp_path):
    async def body(_app: App, window: Window) -> None:
        await open_tab(window, "alpha")
        await window.click('[data-project="alpha"] .group-head')
        folded = "document.querySelector('[data-project=\"alpha\"]').classList.contains('folded')"
        await window.wait(folded, "the group to fold")
        assert (
            await window.js("document.querySelectorAll('[data-project=\"alpha\"] .tab').length")
            == 0
        )
        assert await window.js("document.querySelector('.group-count').textContent") == "1"

        # The "+" in a folded heading opens the dialog without unfolding by itself.
        await window.click('[data-project="alpha"] .group-add')
        assert await window.js(folded) is True
        await window.click("#new-tab-cancel")

        await window.click('[data-project="alpha"] .group-head')
        count = "document.querySelectorAll('[data-project=\"alpha\"] .tab').length"
        await window.wait(f"{count} === 1", "the group to unfold")

    scenario(tmp_path, body)


def test_an_empty_group_has_no_fold_toggle(tmp_path):
    async def body(_app: App, window: Window) -> None:
        await open_tab(window, "alpha")
        group = '[data-project="alpha"]'
        chevron_shown = (
            f"getComputedStyle(document.querySelector('{group} .group-fold')).visibility"
            " === 'visible'"
        )
        assert await window.js(chevron_shown) is True

        await window.click(f"{group} .tab-close")
        tabs = f"document.querySelectorAll('{group} .tab').length"
        await window.wait(f"{tabs} === 0", "the group's last tab to close")
        assert await window.js(chevron_shown) is False
        assert await window.js(f"document.querySelector('{group} .group-head').role") is None

        # Clicking the heading of the kept, empty group does not fold it.
        await window.click(f"{group} .group-head")
        assert (
            await window.js(f"document.querySelector('{group}').classList.contains('folded')")
            is False
        )

    scenario(tmp_path, body)


def test_the_sidebar_keeps_the_width_the_user_gave_it(tmp_path):
    width = "document.getElementById('sidebar').getBoundingClientRect().width"

    async def body(_app: App, window: Window) -> None:
        tab_id = await open_tab(window, "alpha")
        before = await window.js(width)
        # As wide as a session's title can ask for: it must be cut, not widen the column.
        await window.js(
            f"document.querySelector('[data-id=\"{tab_id}\"] .tab-title').textContent ="
            " 'long title '.repeat(40)"
        )
        assert await window.js(width) == before
        await window.click('[data-project="alpha"] .group-head')
        folded = "document.querySelector('[data-project=\"alpha\"]').classList.contains('folded')"
        await window.wait(folded, "the group to fold")
        assert await window.js(width) == before

        await window.drag_by("#sidebar-edge", 80)
        await window.wait(f"{width} === {before + 80}", "the sidebar to widen")
        # Whatever the pointer asks for, it stays wide enough to use.
        await window.drag_by("#sidebar-edge", -340)
        await window.wait(f"{width} === 180", "the sidebar to stop at its least")
        await window.js(
            "document.getElementById('sidebar-edge')"
            ".dispatchEvent(new MouseEvent('dblclick', {bubbles: true}))"
        )
        await window.wait(f"{width} === {before}", "the default width")

    scenario(tmp_path, body)


def test_a_cut_tab_title_shows_in_full_on_hover(tmp_path):
    async def body(_app: App, window: Window) -> None:
        tab_id = await open_tab(window, "alpha")
        title = f"document.querySelector('[data-id=\"{tab_id}\"] .tab-title')"
        hover = f"{title}.dispatchEvent(new MouseEvent('mouseenter'))"

        # A title that fits leaves the row's own hint to show.
        await window.js(hover)
        assert await window.js(f"{title}.hasAttribute('title')") is False

        long = "long title " * 40
        await window.js(f"{title}.textContent = {json.dumps(long)}")
        await window.js(hover)
        assert await window.js(f"{title}.title") == long

    scenario(tmp_path, body)


def test_a_tab_says_how_long_ago_it_was_updated(tmp_path):
    async def body(_app: App, window: Window) -> None:
        tab = await open_tab(window, "alpha")
        label = f"document.querySelector('[data-id=\"{tab}\"] .tab-time')"
        assert await window.js(f"{label}.textContent") == "just now"
        await window.type_into(tab, "finish\r")
        await window.wait_class(tab, has("finished"), "a turn finished")
        assert await window.js(f"{label}.textContent") == "just now"

        cases = {
            "30": "just now",
            "90": "1 minute ago",
            str(5 * 3600): "5 hours ago",
            str(86400): "1 day ago",
            str(7 * 86400): "1 week ago",
            str(15 * 86400): "2 weeks ago",
            str(60 * 86400): "2 months ago",
            str(400 * 86400): "1 year ago",
            "-5": "just now",
        }
        labels = await window.js(
            "import('./sidebar.js').then(m => "
            f"{json.dumps(list(cases))}.map(s => m.timeAgo(1000, 1000 + Number(s))))"
        )
        assert labels == list(cases.values())

    scenario(tmp_path, body)


def test_a_usage_limit_says_when_it_resets_in_the_users_own_clock(tmp_path):
    async def body(_app: App, window: Window) -> None:
        soon, later, past = await window.js(
            "import('./sidebar.js').then(m => {"
            " const now = new Date(2026, 0, 5, 12).getTime() / 1000;"
            " return [now + 3600, now + 3 * 86400, now - 60].map(t => m.resetText(t, now)); })"
        )
        assert soon.startswith("limit \N{MIDDLE DOT} resets ")
        assert later.startswith("limit \N{MIDDLE DOT} resets ")
        # Another day's reset names the day; today's is the time alone.
        assert len(later.split()) == len(soon.split()) + 1
        assert past.startswith("limit reset ")

    scenario(tmp_path, body)


def test_closing_a_group_asks_then_closes_every_session_in_it(tmp_path):
    async def body(_app: App, window: Window) -> None:
        kept = await open_tab(window, "beta")
        await open_tab(window, "alpha")
        await open_tab(window, "alpha")
        await window.click('[data-project="alpha"] .group-close')
        assert await window.js("document.getElementById('confirm').open") is True
        await window.click("#confirm-cancel")
        assert len(await window.tabs()) == 3

        await window.click('[data-project="alpha"] .group-close')
        await window.click("#confirm-ok")
        await window.wait("window.__agentTerminal.tabs().length === 1", "the group's tabs to close")
        assert [tab["id"] for tab in await window.tabs()] == [kept]
        groups = "[...document.querySelectorAll('.group')].map(g => g.dataset.project)"
        assert await window.js(groups) == ["beta"]

    scenario(tmp_path, body)


def test_a_repository_on_github_can_be_cloned_from_the_dialog(tmp_path):
    async def body(app: App, window: Window) -> None:
        records = [
            {"name": name, "nameWithOwner": f"me/{name}", "description": "", "isArchived": False}
            for name in ("alpha", "gamma")
        ]
        app.gh_repos.write_text(json.dumps(records), encoding="utf-8")
        await window.click("#new-group")
        await window.click("#remote-toggle")
        rows = "[...document.querySelectorAll('.remote-repo')].map(r => r.dataset.repo)"
        await window.wait(f"{rows}.length", "the user's repositories")
        assert await window.js(rows) == ["me/gamma"], "alpha is already here"

        await window.click('[data-repo="me/gamma"] .remote-clone')
        await window.wait(f"{rows}.length === 0", "the clone to finish")
        assert (app.projects / "gamma" / ".git").is_dir()
        selected = "document.querySelector('.project.selected')?.textContent"
        assert await window.wait(selected, "the clone to be chosen") == "gamma"
        await window.start_session()
        await window.wait("window.__agentTerminal.tabs().length === 1", "a session in the clone")

    scenario(tmp_path, body)


# devkit's new-project.py as far as the dialog can tell: the same flags, and a folder
# where it was asked for once applied. Nothing is rendered and nothing reaches GitHub.
FAKE_NEW_PROJECT = """
import argparse, pathlib
parser = argparse.ArgumentParser()
parser.add_argument("name")
parser.add_argument("--description", default="")
parser.add_argument("--preset", choices=["bare", "service"])
parser.add_argument("--parent", default="")
parser.add_argument("--remote", action="store_true")
parser.add_argument("--no-remote", action="store_true")
parser.add_argument("--dry-run", action="store_true")
parser.add_argument("--yes", action="store_true")
args = parser.parse_args()
print(f"plan for {args.name}: {args.preset}, remote={args.remote}")
if args.yes:
    (pathlib.Path(args.parent) / args.name).mkdir()
    print("Done")
"""


def test_a_new_project_can_be_made_from_the_dialog(tmp_path):
    async def body(app: App, window: Window) -> None:
        (app.devkit / "scripts").mkdir(parents=True)
        (app.devkit / "scripts" / "new-project.py").write_text(FAKE_NEW_PROJECT, "utf-8")
        await window.click("#new-group")
        await window.click("#scaffold-toggle")
        presets = "[...document.querySelectorAll('#scaffold-preset option')].map(o => o.value)"
        await window.wait(f"{presets}.length", "the presets")
        assert await window.js(presets) == ["service", "bare"]

        await window.js(
            "(() => { const n = document.getElementById('scaffold-name');"
            " n.value = 'fresh'; n.dispatchEvent(new Event('input')); })()"
        )
        await window.click("#scaffold-preview")
        output = "document.getElementById('scaffold-output').textContent"
        await window.wait(f"{output}.includes('plan for fresh')", "the plan")
        assert not (app.projects / "fresh").exists()

        await window.wait("!document.getElementById('scaffold-create').disabled", "the preview")
        await window.click("#scaffold-create")
        selected = "document.querySelector('.project.selected')?.textContent"
        assert await window.wait(selected, "the project to be chosen") == "fresh"
        assert (app.projects / "fresh").is_dir()

    scenario(tmp_path, body)


def test_deleting_a_project_says_what_is_unsaved_and_refuses_one_in_use(tmp_path):
    async def body(app: App, window: Window) -> None:
        await open_tab(window, "alpha")
        await window.click("#new-group")
        delete = "[...document.querySelectorAll('.project-item')].find(i => i.textContent === {0}).querySelector('.project-delete').click()"
        await window.js(delete.format(json.dumps("beta")))
        await window.wait("document.getElementById('confirm').open", "the confirmation")
        text = await window.js("document.getElementById('confirm-text').textContent")
        assert "Recycle Bin" in text
        assert "isn't a git repository" in text
        # Cancelled: nothing here may reach the real Recycle Bin.
        await window.click("#confirm-cancel")

        await window.js(delete.format(json.dumps("alpha")))
        message = "document.getElementById('project-message')"
        await window.wait(f"!{message}.hidden", "the refusal")
        assert "close it first" in await window.js(f"{message}.textContent")
        assert await window.js("document.getElementById('confirm').open") is False
        # Kept after the dialog closes: the line in it is gone with it.
        assert "alpha has a tab open; close it first" in app_log(app)

    scenario(tmp_path, body)


FAKE_RETIRE_PROJECT = """
import json, os, pathlib, sys
verb, name = sys.argv[1], sys.argv[2]
print(f"  {verb} {name} will:")
print("    unplug from the registry" if verb == "retire" else "    plug into the registry")
if "--yes" in sys.argv and verb == "restore":
    (pathlib.Path(os.environ["AGENT_TERMINAL_PROJECTS_ROOT"]) / name).mkdir()
    record = pathlib.Path(__file__).resolve().parents[1] / "logs" / "retired-projects.json"
    record.write_text(json.dumps({}), encoding="utf-8")
"""


def install_retire(app: App, *retired: str) -> None:
    (app.devkit / "scripts").mkdir(parents=True, exist_ok=True)
    (app.devkit / "scripts" / "retire-project.py").write_text(FAKE_RETIRE_PROJECT, "utf-8")
    (app.devkit / "logs").mkdir(exist_ok=True)
    record = {name: {"retired_at": "2026-11-25T18:00:00"} for name in retired}
    (app.devkit / "logs" / "retired-projects.json").write_text(json.dumps(record), "utf-8")


def test_deleting_says_what_devkit_will_retire_first(tmp_path):
    async def body(app: App, window: Window) -> None:
        install_retire(app)
        await window.click("#new-group")
        delete = "[...document.querySelectorAll('.project-item')].find(i => i.textContent === 'beta').querySelector('.project-delete').click()"
        await window.js(delete)
        await window.wait("document.getElementById('confirm').open", "the confirmation")
        text = await window.js("document.getElementById('confirm-text').textContent")
        assert "retire-project.py will:" in text
        assert "unplug from the registry" in text
        # Cancelled: nothing here may reach the real Recycle Bin.
        await window.click("#confirm-cancel")

    scenario(tmp_path, body)


def test_a_retired_project_is_restored_from_the_dialog(tmp_path):
    async def body(app: App, window: Window) -> None:
        install_retire(app, "gone")
        await window.click("#new-group")
        await window.click("#retired-toggle")
        row = "document.querySelector('#retired-list [data-project=\"gone\"] button')"
        await window.wait(row, "the retired project")
        await window.js(f"{row}.click()")
        await window.wait("document.getElementById('confirm').open", "the confirmation")
        await window.click("#confirm-ok")
        selected = "document.querySelector('.project.selected')?.textContent"
        assert await window.wait(selected, "the restored project to be chosen") == "gone"
        assert (app.projects / "gone").is_dir()
        empty = "document.getElementById('retired-status').textContent"
        await window.wait(f"{empty}.includes('Nothing is retired')", "the emptied list")

    scenario(tmp_path, body)


def app_log(app: App) -> str:
    log = app.base / "data" / "agent-terminal.log"
    return log.read_text(encoding="utf-8") if log.exists() else ""


def test_a_script_error_on_the_page_reaches_the_app_log(tmp_path):
    async def body(app: App, window: Window) -> None:
        await window.js("setTimeout(() => { throw new Error('probe-page-error'); })")
        # `void`, so the evaluation does not await it and the rejection goes unhandled.
        await window.js("void Promise.reject(new Error('probe-rejection'))")
        deadline = asyncio.get_running_loop().time() + 10
        while "probe-rejection" not in app_log(app):
            if asyncio.get_running_loop().time() > deadline:
                pytest.fail(f"page errors never reached the log:\n{app_log(app)}")
            await asyncio.sleep(0.2)
        text = app_log(app)
        assert "page error: Uncaught Error: probe-page-error" in text
        assert "page error: unhandled rejection: probe-rejection" in text

    scenario(tmp_path, body)


def test_a_folder_outside_git_says_why_and_can_be_made_ready(tmp_path):
    async def body(_app: App, window: Window) -> None:
        (tmp_path / "projects" / "beta" / "kept.txt").write_text("x", encoding="utf-8")
        await window.choose_project("beta")
        assert await window.js("document.getElementById('worktree').disabled") is True
        assert await window.js("document.getElementById('worktree').checked") is False
        assert "isn't one" in await window.notes()

        await window.click("#prepare-worktree")
        await window.wait("!document.getElementById('worktree').disabled", "git to be set up")
        assert "isn't one" not in await window.notes()
        assert await window.js("document.getElementById('prepare-worktree').hidden") is True
        # The default comes back once the folder can take a worktree.
        assert await window.js("document.getElementById('worktree').checked") is True

        await window.start_session(worktree=None)
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


def test_the_dialog_holds_its_size_and_shows_the_whole_selection(tmp_path):
    rect = "document.getElementById({}).getBoundingClientRect()"

    async def body(_app: App, window: Window) -> None:
        repository = tmp_path / "untrusted"
        make_repository(repository)
        await window.click("#new-group")
        before = await window.js(f"{rect.format(json.dumps('new-tab'))}.toJSON()")

        await window.type_path(str(repository))
        await window.wait("!document.getElementById('folder-notes').hidden", "the trust note")
        after = await window.js(f"{rect.format(json.dumps('new-tab'))}.toJSON()")
        assert (after["top"], after["height"]) == (before["top"], before["height"])

        # The list scrolls, so it clips: the selected outline (2px) must fit inside it.
        await window.js("document.getElementById('new-tab').close()")
        await window.choose_project("alpha")
        fits = await window.js(
            "(() => { const list = document.getElementById('project-list')"
            ".getBoundingClientRect();"
            "const b = document.querySelector('.project.selected').getBoundingClientRect();"
            "return b.left - 2 >= list.left && b.right + 2 <= list.right"
            " && b.top - 2 >= list.top; })()"
        )
        assert fits is True

    scenario(tmp_path, body)


def test_a_first_visit_asks_for_trust_then_loops_back_to_the_worktree(tmp_path):
    async def body(app: App, window: Window) -> None:
        repository = tmp_path / "untrusted"
        make_repository(repository)
        await window.choose_path(repository)
        await window.dialog_ready()
        await window.set_worktree(True)
        await window.wait("document.getElementById('folder-notes').textContent", "a warning")
        assert "worktree starts right after" in await window.notes()

        # Codex asks for itself, so the promise is Claude's alone.
        await window.click("input[name=agent][value=codex]")
        await window.wait(
            "!document.getElementById('folder-notes').textContent.includes('Claude')", "it"
        )
        assert "Codex will ask" in await window.notes()
        assert "starts right after" not in await window.notes()

        await window.start_session(worktree=None)
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
        await window.set_worktree(True)
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
        assert await window.js(icon_in('[data-pr="505"] .tab-dot')) == "pr-open"

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
        on_chip = icon_in(f'[data-id="{tab}"] .tab-chip')
        assert await window.js(on_chip) == "ship"

        # Reaped: the PR it became sits on the tab, and not as a row of its own.
        (alpha / "logs" / "ship-intent.md").unlink()
        write_prs(app, make_pr(9, "worktree-task"))
        await window.wait(f"{chip}?.textContent?.startsWith('PR #9')", "the PR on the tab")
        assert await window.js("document.querySelector('[data-pr]')") is None
        # Its gate passed: the check says so, in place of the text's old tick.
        assert await window.js(f"{chip}.textContent") == "PR #9"
        assert await window.js(on_chip) == "check"

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

        # Each thing that judges it has an icon; nothing is live before the merge.
        gates = f"[...document.querySelectorAll('[data-id=\"{tab}\"] .gate')].map(g => g.className)"
        await window.wait(f"{gates}.length === 2", "the indicators")
        assert await window.js(gates) == ["gate passing", "gate pending"]

        # Merged on GitHub: the tab says so.
        write_prs(app, make_pr(9, "worktree-task", state="MERGED"))
        await window.wait(f"{chip}?.textContent === 'PR #9 merged'", "the merged chip")
        assert await window.js(on_chip) == "pr-merged"

    scenario(tmp_path, body)


def test_a_tab_with_unshipped_work_offers_to_land_it(tmp_path):
    async def body(_app: App, window: Window) -> None:
        alpha = tmp_path / "projects" / "alpha"
        skill = alpha / ".claude" / "skills" / "land" / "SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text("land", encoding="utf-8")
        git(alpha, "add", "--all")
        git(alpha, "commit", "--message", "skill")
        git(alpha, "update-ref", "refs/remotes/origin/main", "HEAD")
        git(alpha, "switch", "--create", "worktree-task")
        tab = await open_tab(window, "alpha")
        button = f"document.querySelector('[data-id=\"{tab}\"] .land')"
        assert await window.js(button) is None

        (alpha / "new.txt").write_text("x", encoding="utf-8")
        await window.wait(f"{button}?.textContent === 'Create PR'", "the button")
        await window.js(f"{button}.click()")
        screen = f"window.__agentTerminal.screen('{tab}')"
        await window.wait(f"{screen}.includes('/land')", "the skill typed to the agent")

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


def test_a_groups_plus_offers_its_unopened_sessions_with_their_state(tmp_path):
    async def body(app: App, window: Window) -> None:
        alpha = tmp_path / "projects" / "alpha"
        tree = alpha / ".claude" / "worktrees" / "earlier"
        git(alpha, "worktree", "add", str(tree), "-b", "worktree-earlier")
        encoded = "".join(c if c.isalnum() else "-" for c in str(tree))
        transcript = tmp_path / "claude-home" / "projects" / encoded / "earlier-session.jsonl"
        transcript.parent.mkdir(parents=True)
        title = {"type": "ai-title", "aiTitle": "Earlier <b>work</b>", "sessionId": "x"}
        transcript.write_text(json.dumps(title) + "\n", encoding="utf-8")
        (tree / "logs").mkdir()
        (tree / "logs" / "ship-intent.md").write_text("Ship it\n", encoding="utf-8")
        write_prs(app, make_pr(12, "worktree-earlier"))
        first = await open_tab(window, "alpha")
        await window.wait("window.__agentTerminal.tabs()[0].session_id !== null", "an id")

        await window.click('[data-project="alpha"] .group-add')
        start = "document.getElementById('new-tab-start').getBoundingClientRect().top"
        start_while_loading = await window.js(start)
        row = "document.querySelector('.past-session[data-session=\"earlier-session\"]')"
        await window.wait(f"{row}?.textContent?.includes('PR #12')", "the session and its PR")
        # The sessions arriving must not move Start out from under the pointer.
        assert await window.js(start) == start_while_loading
        # The title is text, never markup; the state reads intent, then PR.
        assert await window.js(f"{row}.querySelector('.tab-title').textContent") == (
            "Earlier <b>work</b>"
        )
        chips = f"[...{row}.querySelectorAll('.tab-chip')].map(c => c.textContent)"
        assert await window.js(chips) == ["ship queued", "PR #12"]
        # The tab already open is not offered.
        own = (await window.tabs())[0]["session_id"]
        assert await window.js(f"!!document.querySelector('[data-session=\"{own}\"]')") is False

        await window.js(f"{row}.click()")
        await window.wait("window.__agentTerminal.tabs().length === 2", "the resumed tab")
        await window.wait("!document.getElementById('new-tab').open", "the dialog to close")
        await wait_launches(app, 2)
        launch = launches(app)[-1]
        assert launch["argv"] == ["--resume", "earlier-session"]
        assert Path(launch["cwd"]) == tree
        assert first in {tab["id"] for tab in await window.tabs()}

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


def test_a_sleeping_tab_shows_a_moon_and_resumes_when_clicked(tmp_path):
    make_projects(tmp_path)
    project = tmp_path / "projects" / "alpha"
    transcript = tmp_path / "claude-home" / "projects" / "encoded" / "s.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text("{}\n", encoding="utf-8")
    saved = {"project": "alpha", "project_path": str(project), "agent": "claude", "created_at": 1.0}
    tabs = [
        {**saved, "id": "awake", "session_id": "other"},
        {**saved, "id": "resting", "session_id": "s", "asleep": True},
    ]
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "state.json").write_text(
        json.dumps({"version": 1, "active_id": "awake", "tabs": tabs}), encoding="utf-8"
    )

    async def run() -> None:
        app = App(tmp_path)
        try:
            window = await app.start()
            await window.wait_class("awake", has("idle"), "ready")
            await window.wait_class("resting", has("asleep"), "asleep")
            mark = '[data-id="resting"] .tab-dot'
            assert await window.js(icon_in(mark)) == "asleep"
            meta = "document.querySelector('[data-id=\"resting\"] .tab-meta').textContent"
            assert "asleep" in await window.js(meta)
            assert len(launches(app)) == 1

            await window.click('[data-id="resting"]')
            await window.wait_class("resting", has("idle"), "resumed")
            assert launches(app)[-1]["argv"] == ["--resume", "s"]
        finally:
            await app.dispose()
            app.kill()

    asyncio.run(run())


def test_a_restart_keeps_the_agent_running_mid_turn(tmp_path):
    async def run() -> None:
        make_projects(tmp_path)
        app = App(tmp_path)
        try:
            window = await app.start()
            tab = await open_tab(window, "alpha")
            await window.type_into(tab, "work\r")
            await window.wait_class(tab, has("working"), "working")
            [agent] = [launch["pid"] for launch in launches(app)]
            before = app.app_pid()

            await window.js("window.__agentTerminal.send({type: 'app_restart', update: false})")
            window = await app.follow_restart(before)

            await window.wait("window.__agentTerminal.tabs().length === 1", "the tab back")
            assert len(launches(app)) == 1, "the agent was started again, not kept"
            assert process_running(agent), "the agent did not survive the restart"
            await window.wait_class(tab, has("working"), "still working")
            screen = f"window.__agentTerminal.screen({json.dumps(tab)})"
            await window.wait(f"{screen}.includes('fake agent ready')", "the screen replayed")
            sessions = await window.wait("window.__agentTerminal.app()?.sessions", "app status")
            assert sessions == {"kept": True, "stale": False}

            # The same process, still answering: input reaches it and its state comes back.
            await window.type_into(tab, "finish\r")
            # Fails if the replayed screen's old queries were answered into the agent.
            await window.wait_class(tab, has("finished"), "finished")

            host = app.host_pid()
            await app.close_followed_window()
            assert not process_running(agent), "the agent outlived the window"
            for _ in range(100):
                if app.host_pid() is None:
                    break
                await asyncio.sleep(0.1)
            assert app.host_pid() is None, "the session host outlived the window"
            assert host is not None and not process_running(host)
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


def test_the_prompt_between_claudes_rules_is_shaded(tmp_path):
    async def body(_app: App, window: Window) -> None:
        tab = await open_tab(window, "alpha")
        shade = f"window.__agentTerminal.promptShade({json.dumps(tab)})"
        assert await window.js(shade) is None, "nothing is boxed in before the prompt is drawn"
        await window.type_into(tab, "compose\r")
        rows = await window.wait(shade, "the prompt to be shaded")
        assert rows["start"] == rows["end"], "only the prompt line lies between the rules"
        screen = (await window.js(f"window.__agentTerminal.screen({json.dumps(tab)})")).split("\n")
        assert screen[rows["start"]].startswith(PROMPT), screen

    scenario(tmp_path, body)


def test_right_click_leaves_the_paste_to_an_agent_tracking_the_mouse(tmp_path):
    """Claude Code pastes on a right-click it is sent, so the app pasting too doubled it."""

    async def body(_app: App, window: Window) -> None:
        tab = await open_tab(window, "alpha")
        mode = f"window.__agentTerminal.mouseMode({json.dumps(tab)})"
        assert await window.js(mode) == "none"
        await window.type_into(tab, "mouse\r")
        await window.wait(f"{mode} !== 'none'", "the agent to turn mouse tracking on")
        # What the page sends, recorded as it sends it: the paste request is synchronous.
        await window.js(
            "(() => { window.__sent = []; const send = WebSocket.prototype.send;"
            " WebSocket.prototype.send = function (data) {"
            " window.__sent.push(JSON.parse(data).type); return send.call(this, data); };"
            " })()"
        )
        await window.right_click(".term-host.shown .xterm")
        assert "paste" not in await window.js("window.__sent")

    scenario(tmp_path, body)


def test_the_tally_counts_today_and_opens_the_graph(tmp_path):
    async def body(app: App, window: Window) -> None:
        now = datetime.now().astimezone()
        home = app.base / "claude-home"
        home.mkdir(exist_ok=True)
        records = [
            {
                "display": text,
                "timestamp": now.timestamp() * 1000 - 1,
                "project": "C:\\r\\.claude\\worktrees\\mine",
                "sessionId": "s",
            }
            for text in ("a", "b")
        ]
        lines = "".join(json.dumps(record) + "\n" for record in records)
        (home / "history.jsonl").write_text(lines, encoding="utf-8")
        mine = make_pr(7, "worktree-mine", createdAt=now.isoformat())
        write_prs(app, mine, make_pr(8, "agent/auto/upgrade", createdAt=now.isoformat()))
        today = "[...document.querySelectorAll('.tally-today')].map((n) => n.textContent).join()"
        await window.wait(f"{today} === '2,1'", "today's prompts and PRs")
        await window.click("#tally")
        await window.wait("document.getElementById('productivity').open", "the graph")
        assert await window.js("document.querySelectorAll('.chart-bar').length") == 2
        # Both the prompts, typed in r's worktree, and the PR, in o/r, are r's.
        options = "[...document.querySelectorAll('#productivity-project option')]"
        labels = await window.js(f"{options}.map((n) => n.textContent)")
        assert labels == ["All projects", "r: 2 prompts · 1 PR today"]
        await window.js(
            "(() => { const p = document.getElementById('productivity-project');"
            "p.value = 'r'; p.dispatchEvent(new Event('change')); })()"
        )
        assert await window.js("document.querySelectorAll('.chart-bar').length") == 2
        await window.click("#productivity-close")
        await window.wait("!document.getElementById('productivity').open", "the graph to close")

    scenario(tmp_path, body)


def test_a_group_heading_counts_its_projects_day(tmp_path):
    async def body(app: App, window: Window) -> None:
        await open_tab(window, "alpha")
        now = datetime.now().astimezone()
        home = app.base / "claude-home"
        home.mkdir(exist_ok=True)
        worktree = str(app.projects / "alpha" / ".claude" / "worktrees" / "mine")
        # The third is typed in the projects root itself, so it is no project's but its own.
        typed = (("a", worktree), ("b", worktree), ("c", str(app.projects)))
        records = [
            {
                "display": text,
                "timestamp": now.timestamp() * 1000 - 1,
                "project": folder,
                "sessionId": "s",
            }
            for text, folder in typed
        ]
        lines = "".join(json.dumps(record) + "\n" for record in records)
        (home / "history.jsonl").write_text(lines, encoding="utf-8")
        url = "https://github.com/o/alpha/pull/7"
        write_prs(app, make_pr(7, "worktree-mine", url=url, createdAt=now.isoformat()))
        badge = '[data-project="alpha"] .group-today span'
        counts = f"[...document.querySelectorAll('{badge}')].map((n) => n.textContent).join()"
        await window.wait(f"{counts} === '2,1'", "alpha's prompts and PRs today")
        title = "document.querySelector('[data-project=\"alpha\"] .group-today').title"
        assert await window.js(title) == "Today in alpha: 2 prompts and 1 PR"

    scenario(tmp_path, body)


def test_a_second_launch_hands_over_instead_of_doubling(tmp_path):
    def launch_again(app: App) -> int:
        second = subprocess.run(app.command(), cwd=app.root, env=app.env(), timeout=30, check=False)
        return second.returncode

    async def body(app: App, _window: Window) -> None:
        assert await asyncio.to_thread(launch_again, app) == 0
        assert app.process is not None and app.process.poll() is None

    scenario(tmp_path, body)
