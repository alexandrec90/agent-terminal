"""Stands in for `gh` in the end-to-end tests, so no test can touch a real pull request.

Holds PRs in the JSON file `FAKE_GH_STATE` names and logs each mutating call to
`FAKE_GH_LOG`. Records have the shape `gh pr list --json` really returns.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

DIFF = """diff --git a/app.py b/app.py
index 1111111..2222222 100644
--- a/app.py
+++ b/app.py
@@ -1,2 +1,2 @@
 unchanged
-removed line
+added <b>line</b>
"""


def load() -> list[dict[str, Any]]:
    path = Path(os.environ["FAKE_GH_STATE"])
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else []


def save(records: list[dict[str, Any]]) -> None:
    Path(os.environ["FAKE_GH_STATE"]).write_text(json.dumps(records), encoding="utf-8")


def log(args: list[str]) -> None:
    with Path(os.environ["FAKE_GH_LOG"]).open("a", encoding="utf-8") as handle:
        handle.write(" ".join(args) + "\n")


def option(args: list[str], name: str) -> str | None:
    return args[args.index(name) + 1] if name in args else None


def pr_list(args: list[str]) -> int:
    records = load()
    head = option(args, "--head")
    if head is not None:
        records = [record for record in records if record["headRefName"] == head]
    if option(args, "--state") == "open":
        records = [record for record in records if record["state"] == "OPEN"]
    print(json.dumps(records))
    return 0


def pr_view(args: list[str]) -> int:
    number = int(args[0])
    record = next(r for r in load() if r["number"] == number)
    print(json.dumps({"additions": 1, "deletions": 1, "changedFiles": 1, **record}))
    return 0


def pr_diff(_args: list[str]) -> int:
    print(DIFF)
    return 0


def set_state(args: list[str], state: str) -> int:
    log(["pr", state, *args])
    number = int(args[0])
    records = load()
    record = next(r for r in records if r["number"] == number)
    if record.get("refuseMerge"):
        print(record["refuseMerge"], file=sys.stderr)
        return 1
    record["state"] = state
    save(records)
    return 0


def main(argv: list[str]) -> int:
    if argv[:1] != ["pr"]:
        print(f"fake gh does not know {argv}", file=sys.stderr)
        return 2
    command, *rest = argv[1:]
    if command == "list":
        return pr_list(rest)
    if command == "view":
        return pr_view(rest)
    if command == "diff":
        return pr_diff(rest)
    if command == "merge":
        return set_state(rest, "MERGED")
    if command == "close":
        return set_state(rest, "CLOSED")
    print(f"fake gh does not know {argv}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
