import json
import subprocess
from types import SimpleNamespace

import pytest

from agent_terminal import prs
from agent_terminal.prs import (
    CHECKS_FAILING,
    CHECKS_NONE,
    CHECKS_PASSING,
    CHECKS_PENDING,
    Gh,
    GhError,
    GhMissing,
    check_outcome,
    check_rows,
    parse_pull_requests,
    summarize_checks,
    to_pull_request,
)
from agent_terminal.settings import Settings

RUN_OK = {"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "SUCCESS", "name": "gate"}
RUN_SKIPPED = {**RUN_OK, "conclusion": "SKIPPED", "name": "automerge"}
RUN_FAILED = {**RUN_OK, "conclusion": "FAILURE"}
RUN_GOING = {"__typename": "CheckRun", "status": "IN_PROGRESS", "conclusion": "", "name": "gate"}
RECORD = {
    "number": 505,
    "title": "Frame the panel",
    "url": "https://github.com/o/r/pull/505",
    "state": "OPEN",
    "headRefName": "worktree-quizzical",
    "baseRefName": "master",
    "isDraft": False,
    "author": {"login": "someone", "is_bot": False},
    "statusCheckRollup": [RUN_OK, RUN_SKIPPED],
    "reviewDecision": "",
    "mergeable": "MERGEABLE",
    "labels": [{"name": "automerge"}],
}


class TestChecks:
    @pytest.mark.parametrize(
        ("check", "outcome"),
        [
            (RUN_OK, CHECKS_PASSING),
            (RUN_SKIPPED, CHECKS_PASSING),
            (RUN_FAILED, CHECKS_FAILING),
            ({**RUN_OK, "conclusion": "TIMED_OUT"}, CHECKS_FAILING),
            (RUN_GOING, CHECKS_PENDING),
            ({"__typename": "StatusContext", "state": "SUCCESS"}, CHECKS_PASSING),
            ({"__typename": "StatusContext", "state": "ERROR"}, CHECKS_FAILING),
            ({"__typename": "StatusContext", "state": "PENDING"}, CHECKS_PENDING),
        ],
    )
    def test_one_check(self, check, outcome):
        assert check_outcome(check) == outcome

    @pytest.mark.parametrize(
        ("rollup", "verdict"),
        [
            ([RUN_OK, RUN_SKIPPED], CHECKS_PASSING),
            ([RUN_OK, RUN_GOING], CHECKS_PENDING),
            ([RUN_FAILED, RUN_GOING, RUN_OK], CHECKS_FAILING),
            ([], CHECKS_NONE),
            (None, CHECKS_NONE),
        ],
    )
    def test_all_together(self, rollup, verdict):
        assert summarize_checks(rollup) == verdict

    def test_rows_leave_out_skipped_checks(self):
        rows = check_rows([{**RUN_OK, "detailsUrl": "https://x"}, RUN_SKIPPED])
        assert rows == [{"name": "gate", "outcome": CHECKS_PASSING, "url": "https://x"}]

    def test_rows_read_a_status_context(self):
        status = {
            "__typename": "StatusContext",
            "state": "SUCCESS",
            "context": "ci/x",
            "targetUrl": "https://y",
        }
        assert check_rows([status])[0] == {
            "name": "ci/x",
            "outcome": CHECKS_PASSING,
            "url": "https://y",
        }


class TestParsing:
    def test_reads_a_real_shaped_record(self):
        pr = to_pull_request(RECORD)
        assert (pr.number, pr.head, pr.base) == (505, "worktree-quizzical", "master")
        assert pr.author == "someone"
        assert pr.checks == CHECKS_PASSING
        assert pr.automerge is True

    def test_tolerates_what_gh_leaves_empty(self):
        sparse = {k: RECORD[k] for k in ("number", "title", "url", "state", "headRefName")}
        pr = to_pull_request({**sparse, "author": None, "labels": None})
        assert (pr.author, pr.base, pr.draft, pr.automerge) == ("", "", False, False)

    def test_a_list_reply(self):
        assert [p.number for p in parse_pull_requests(json.dumps([RECORD]))] == [505]

    @pytest.mark.parametrize("reply", ["not json", "[{}]", "[1]", "{"])
    def test_anything_else_is_an_error(self, reply):
        with pytest.raises(GhError):
            parse_pull_requests(reply)


class FakeRun:
    """Stands in for `subprocess.run`, replying by subcommand."""

    def __init__(self, replies=None, code=0, stderr=""):
        self.replies = replies or {}
        self.code = code
        self.stderr = stderr
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append(argv)
        key = argv[2] if len(argv) > 2 else ""
        return SimpleNamespace(
            returncode=self.code, stdout=self.replies.get(key, "[]"), stderr=self.stderr
        )


@pytest.fixture
def run(monkeypatch):
    fake = FakeRun()
    monkeypatch.setattr(prs.subprocess, "run", fake)
    return fake


@pytest.fixture
def gh():
    return Gh(Settings(gh_command="gh"))


class TestGh:
    def test_lists_open_prs(self, gh, run, tmp_path):
        run.replies["list"] = json.dumps([RECORD])
        assert gh.list_open(tmp_path)[0].number == 505
        assert run.calls[0][:5] == ["gh", "pr", "list", "--state", "open"]

    def test_looks_a_branch_up_in_every_state(self, gh, run, tmp_path):
        run.replies["list"] = json.dumps([{**RECORD, "state": "MERGED"}])
        assert gh.for_branch(tmp_path, "worktree-x").state == "MERGED"
        call = run.calls[0]
        assert call[call.index("--head") + 1] == "worktree-x"
        assert call[call.index("--state") + 1] == "all"

    def test_a_branch_with_no_pr(self, gh, run, tmp_path):
        assert gh.for_branch(tmp_path, "worktree-x") is None

    def test_merge_squashes_and_leaves_the_branch(self, gh, run, tmp_path):
        gh.merge(tmp_path, 505)
        assert run.calls == [["gh", "pr", "merge", "505", "--squash"]]

    def test_close(self, gh, run, tmp_path):
        gh.close(tmp_path, 505)
        assert run.calls == [["gh", "pr", "close", "505"]]

    def test_a_failure_carries_ghs_words(self, gh, tmp_path, monkeypatch):
        fake = FakeRun(code=1, stderr="Pull request is in clean status but not mergeable")
        monkeypatch.setattr(prs.subprocess, "run", fake)
        with pytest.raises(GhError, match="not mergeable"):
            gh.merge(tmp_path, 1)

    def test_a_silent_failure_says_so(self, gh, tmp_path, monkeypatch):
        monkeypatch.setattr(prs.subprocess, "run", FakeRun(replies={"merge": ""}, code=3))
        with pytest.raises(GhError, match="exited 3"):
            gh.merge(tmp_path, 1)

    def test_a_hang_is_an_error(self, gh, tmp_path, monkeypatch):
        def hang(*_args, **_kwargs):
            raise subprocess.TimeoutExpired("gh", 60)

        monkeypatch.setattr(prs.subprocess, "run", hang)
        with pytest.raises(GhError, match="seconds"):
            gh.list_open(tmp_path)

    def test_no_gh_is_told_apart(self, gh, tmp_path, monkeypatch):
        def absent(*_args, **_kwargs):
            raise FileNotFoundError("gh")

        monkeypatch.setattr(prs.subprocess, "run", absent)
        with pytest.raises(GhMissing):
            gh.list_open(tmp_path)


class TestDetail:
    def test_gathers_the_page(self, gh, run, tmp_path):
        record = {
            **RECORD,
            "body": "Why",
            "additions": 3,
            "deletions": 1,
            "changedFiles": 2,
            "mergeStateStatus": "CLEAN",
            "comments": [{"author": {"login": "a"}, "body": "hi"}],
        }
        run.replies["view"] = json.dumps(record)
        run.replies["diff"] = "diff --git a/x b/x\n"
        detail = gh.detail(tmp_path, 505)
        assert detail["body"] == "Why"
        assert (detail["additions"], detail["deletions"], detail["changed_files"]) == (3, 1, 2)
        assert detail["merge_state"] == "CLEAN"
        assert detail["comments"] == [{"author": "a", "body": "hi"}]
        assert detail["diff"].startswith("diff --git")
        assert detail["diff_truncated"] is False

    def test_cuts_a_huge_diff_and_says_so(self, gh, run, tmp_path, monkeypatch):
        monkeypatch.setattr(prs, "MAX_DIFF_CHARACTERS", 5)
        run.replies["view"] = json.dumps(RECORD)
        run.replies["diff"] = "x" * 6
        detail = gh.detail(tmp_path, 505)
        assert detail["diff"] == "xxxxx"
        assert detail["diff_truncated"] is True

    def test_a_malformed_view_is_an_error(self, gh, run, tmp_path):
        run.replies["view"] = "{}"
        with pytest.raises(GhError):
            gh.detail(tmp_path, 505)
