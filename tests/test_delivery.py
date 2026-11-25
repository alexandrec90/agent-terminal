import json
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from agent_terminal import delivery
from agent_terminal.delivery import (
    DeliveryReader,
    Progress,
    default_ref,
    has_land_skill,
    has_unshipped,
    land_action,
    land_prompt,
    read_tier,
    tier_state,
    worst,
)
from agent_terminal.prs import PullRequest

PR = PullRequest(
    number=7,
    title="t",
    url="u",
    state="OPEN",
    head="task",
    base="main",
    draft=False,
    author="a",
    checks="passing",
    mergeable="MERGEABLE",
    review="",
    automerge=False,
    ci="passing",
)
MERGED = replace(PR, state="MERGED")
CLOSED = replace(PR, state="CLOSED")


def git(folder: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", *args],  # noqa: S607 - fixed test commands
        cwd=folder,
        check=True,
        capture_output=True,
        encoding="utf-8",
    )
    return done.stdout.strip()


def commit(repo: Path, name: str) -> str:
    (repo / name).write_text(name)
    git(repo, "add", "--all")
    git(repo, "commit", "--message", name)
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path):
    """On branch `task`, level with `origin/main`."""
    folder = tmp_path / "repo"
    folder.mkdir()
    git(folder, "init", "--initial-branch", "main")
    git(folder, "config", "user.email", "t@example.com")
    git(folder, "config", "user.name", "T")
    commit(folder, "first")
    git(folder, "update-ref", "refs/remotes/origin/main", "HEAD")
    git(folder, "switch", "--create", "task")
    return folder


@pytest.fixture
def devkit(tmp_path):
    return tmp_path / "devkit"


def write_result(devkit: Path, tier: str, **fields) -> None:
    folder = devkit / "logs" / tier / "repo"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "result.json").write_text(json.dumps(fields))


def states(progress: Progress) -> dict[str, str]:
    return {gate.name: gate.state for gate in progress.gates}


def add_skill(repo: Path, folder: str = ".claude") -> None:
    path = repo / folder / "skills" / "land" / "SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_text("land")


class TestUnshipped:
    def test_nothing_changed(self, repo):
        assert default_ref(repo) == "origin/main"
        assert not has_unshipped(repo, None)

    def test_nowhere_to_ship_to(self, repo):
        git(repo, "update-ref", "-d", "refs/remotes/origin/main")
        (repo / "new").write_text("x")
        assert default_ref(repo) is None
        assert not has_unshipped(repo, None)

    @pytest.mark.parametrize("pr", [None, PR, MERGED])
    def test_an_uncommitted_file_counts(self, repo, pr):
        (repo / "new").write_text("x")
        assert has_unshipped(repo, pr)

    def test_a_commit_counts_with_no_pr_even_once_pushed(self, repo):
        commit(repo, "a")
        git(repo, "update-ref", "refs/remotes/origin/task", "HEAD")
        assert has_unshipped(repo, None)

    def test_what_the_pr_holds_does_not_count(self, repo):
        head = commit(repo, "a")
        assert not has_unshipped(repo, replace(PR, head_oid=head))
        commit(repo, "b")
        assert has_unshipped(repo, replace(PR, head_oid=head))

    def test_a_push_the_poll_has_not_seen_does_not_count(self, repo):
        head = commit(repo, "a")
        commit(repo, "b")
        git(repo, "update-ref", "refs/remotes/origin/task", "HEAD")
        assert not has_unshipped(repo, replace(PR, head_oid=head))

    def test_a_squash_merged_branch_has_shipped(self, repo):
        head = commit(repo, "a")
        assert not has_unshipped(repo, replace(MERGED, head_oid=head))

    def test_an_unknown_pr_commit_says_nothing(self, repo):
        commit(repo, "a")
        assert not has_unshipped(repo, replace(PR, head_oid="a" * 40))

    def test_a_folder_that_has_gone(self, tmp_path):
        assert not has_unshipped(tmp_path / "gone", None)


class TestLand:
    def test_the_skill_is_looked_for_where_the_agent_does(self, repo):
        add_skill(repo, ".agents")
        assert has_land_skill(repo, "codex")
        assert not has_land_skill(repo, "claude")
        assert not has_land_skill(repo, "other")

    def test_the_action_follows_the_pr(self):
        assert land_action(None) == "create"
        assert land_action(CLOSED) == "create"
        assert land_action(PR) == "push"
        assert land_action(MERGED) == "follow_up"

    def test_each_agent_is_asked_in_its_own_way(self):
        assert land_prompt("claude", "create", None) == "/land"
        assert "land skill" in land_prompt("codex", "push", PR)

    @pytest.mark.parametrize("agent", ["claude", "codex"])
    def test_a_follow_up_names_where_the_merged_work_ended(self, agent):
        prompt = land_prompt(agent, "follow_up", replace(MERGED, head_oid="b" * 40))
        assert "#7" in prompt
        assert "b" * 40 in prompt
        assert "land skill" in prompt
        assert "\n" not in prompt

    def test_a_commit_that_is_not_one_is_never_typed(self):
        prompt = land_prompt("claude", "follow_up", replace(MERGED, head_oid="x; rm"))
        assert prompt == "/land"


class TestTiers:
    def test_worst(self):
        assert worst("passing", "pending", "failing") == "failing"
        assert worst("passing", "pending", "none") == "pending"
        assert worst("passing", "none") == "passing"
        assert worst("none") == "none"

    def test_a_result_that_cannot_be_read_is_none(self, devkit):
        results = devkit / "logs" / "e2e"
        assert read_tier(results, "repo") is None
        write_result(devkit, "e2e", status="passed")
        assert read_tier(results, "repo") == {"status": "passed"}
        (results / "repo" / "result.json").write_text("[]")
        assert read_tier(results, "repo") is None
        (results / "repo" / "result.json").write_text("{")
        assert read_tier(results, "repo") is None

    def test_a_run_judges_only_the_commits_it_contains(self, repo):
        before = git(repo, "rev-parse", "HEAD")
        merge = commit(repo, "a")
        after = commit(repo, "b")
        assert tier_state(repo, merge, None) == "pending"
        for sha, status, expected in (
            (before, "passed", "pending"),
            (before, "failed", "pending"),
            (merge, "passed", "passing"),
            (after, "failed", "failing"),
            (after, "skipped", "pending"),
        ):
            assert tier_state(repo, merge, {"sha": sha, "status": status}) == expected
        red_since = {"sha": after, "status": "failed", "last_green": merge}
        assert tier_state(repo, merge, red_since) == "passing"


class TestReader:
    def test_outside_git_there_is_nothing(self, devkit, tmp_path):
        reader = DeliveryReader(devkit, tmp_path)
        assert reader.read(tmp_path, "claude", None, "", None) == Progress()

    def test_the_button_needs_the_skill_the_work_and_no_queued_intent(self, devkit, repo):
        reader = DeliveryReader(devkit, devkit)
        (repo / "new").write_text("x")
        assert reader.read(repo, "claude", repo, "", None).land == ""
        add_skill(repo)
        assert reader.read(repo, "claude", repo, "", None).land == "create"
        assert reader.read(repo, "claude", repo, "", PR).land == "push"
        assert reader.read(repo, "claude", repo, "refused", None).land == "create"
        assert reader.read(repo, "claude", repo, "intent", None).land == ""

    def test_a_project_without_tiers_shows_none(self, devkit, repo):
        reader = DeliveryReader(devkit, devkit)
        assert states(reader.read(repo, "claude", repo, "", PR)) == {
            "ci": "passing",
            "live": "pending",
        }
        assert states(reader.read(repo, "claude", repo, "", CLOSED)) == {"ci": "passing"}

    def test_every_gate_after_the_merge(self, devkit, repo):
        merge = commit(repo, "a")
        write_result(devkit, "medium-tier", status="passed", sha=merge)
        write_result(devkit, "e2e", status="failed", sha=merge)
        reader = DeliveryReader(devkit, devkit)
        assert states(reader.read(repo, "claude", repo, "", PR))["tests"] == "pending"
        merged = replace(MERGED, merge_commit=merge)
        assert states(reader.read(repo, "claude", repo, "", merged)) == {
            "ci": "passing",
            "tests": "passing",
            "e2e": "failing",
            "live": "passing",
        }

    def test_an_owed_tier_that_never_ran_is_pending(self, devkit, repo):
        (repo / "scripts").mkdir()
        (repo / "scripts" / "e2e.py").write_text("")
        reader = DeliveryReader(devkit, devkit)
        assert states(reader.read(repo, "claude", repo, "", MERGED))["e2e"] == "pending"

    def test_a_pass_outlives_a_later_red(self, devkit, repo):
        merge = commit(repo, "a")
        merged = replace(MERGED, merge_commit=merge)
        reader = DeliveryReader(devkit, devkit)
        write_result(devkit, "e2e", status="passed", sha=merge)
        reader.read(repo, "claude", repo, "", merged)
        write_result(devkit, "e2e", status="failed", sha=commit(repo, "b"))
        assert states(reader.read(repo, "claude", repo, "", merged))["e2e"] == "passing"

    def test_the_apps_own_change_is_live_only_after_a_restart(self, devkit, repo):
        reader = DeliveryReader(devkit, repo)
        merged = replace(MERGED, merge_commit=commit(repo, "a"))
        gate = reader.read(repo, "claude", repo, "", merged).gates[-1]
        assert (gate.name, gate.state) == ("live", "pending")
        assert gate.detail == delivery.OWN_LIVE_DETAILS["pending"]
        restarted = DeliveryReader(devkit, repo)
        assert states(restarted.read(repo, "claude", repo, "", merged))["live"] == "passing"
