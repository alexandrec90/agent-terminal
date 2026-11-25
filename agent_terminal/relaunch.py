"""The restart helper: waits for the app to go, updates it if asked, and starts it again.

Started by the app, detached (`launch_relaunch`), as `python -m agent_terminal.relaunch`,
just before it closes its window. It holds nothing of the app's open, so `git pull` and
`uv sync` can replace any file the app used. How an update went is written to
`Settings.update_result_file` for the next window to show, and everything git and uv said
to `Settings.update_log_file`, on failure too. The app is started again either way: a
failed update leaves the checkout as it was, which still runs.
"""

from __future__ import annotations

import argparse
import json
import logging
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import IO

from agent_terminal.detached import spawn_detached
from agent_terminal.instance import acquire_instance_lock
from agent_terminal.settings import Settings

log = logging.getLogger(__name__)

EXIT_WAIT_SECONDS = 60.0
POLL_SECONDS = 0.2
STEP_TIMEOUT_SECONDS = 600
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

Run = Callable[..., subprocess.CompletedProcess[str]]


@dataclass(frozen=True)
class UpdateResult:
    """How an update went, for the window that comes next."""

    ok: bool
    # The step that failed, or the last one, as the user would type it.
    step: str
    detail: str = ""
    at: float = 0.0


def wait_for_exit(
    lock_file: Path,
    timeout: float = EXIT_WAIT_SECONDS,
    acquire: Callable[[Path], IO[str] | None] = acquire_instance_lock,
    sleep: Callable[[float], None] = time.sleep,
) -> bool:
    """Wait until the app has let go of its instance lock, which it does last."""
    waited = 0.0
    while waited < timeout:
        held = acquire(lock_file)
        if held is not None:
            held.close()
            return True
        sleep(POLL_SECONDS)
        waited += POLL_SECONDS
    return False


def update_steps(branch: str, uv: str | None) -> list[list[str]]:
    """What an update runs, in order."""
    steps = [["git", "pull", "--ff-only", "origin", branch]]
    steps.append([uv or "uv", "sync", "--all-extras"])
    return steps


def apply_update(
    checkout: Path, branch: str, log_file: Path, run: Run = subprocess.run, uv: str | None = None
) -> UpdateResult:
    """Fast-forward the checkout and sync its environment, stopping at the first failure.
    Every step's output goes to `log_file`, overwritten for this run."""
    uv = uv if uv is not None else shutil.which("uv")
    transcript: list[str] = []
    result = UpdateResult(ok=True, step="", at=time.time())
    for step in update_steps(branch, uv):
        shown = " ".join(step)
        transcript.append(f"$ {shown}")
        if step[0] == "uv" and uv is None:
            result = UpdateResult(False, shown, "uv is not on PATH", time.time())
            transcript.append(result.detail)
            break
        try:
            done = run(
                step,
                cwd=checkout,
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                timeout=STEP_TIMEOUT_SECONDS,
                creationflags=NO_WINDOW,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            result = UpdateResult(False, shown, str(exc), time.time())
            transcript.append(result.detail)
            break
        said = "\n".join(part for part in (done.stdout, done.stderr) if part).strip()
        transcript.append(said)
        if done.returncode != 0:
            result = UpdateResult(False, shown, said or f"exited {done.returncode}", time.time())
            break
        result = UpdateResult(True, shown, "", time.time())
    log_file.parent.mkdir(parents=True, exist_ok=True)
    log_file.write_text("\n".join(transcript) + "\n", encoding="utf-8")
    return result


def head_commit(checkout: Path, run: Run = subprocess.run) -> str:
    """The checkout's commit, short, or "" if git cannot say."""
    try:
        done = run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=checkout,
            capture_output=True,
            encoding="utf-8",
            timeout=30,
            creationflags=NO_WINDOW,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return done.stdout.strip() if done.returncode == 0 else ""


def relaunch_argv(python: str) -> list[str]:
    """How the app is started again."""
    return [python, "-m", "agent_terminal"]


def launch_relaunch(settings: Settings, checkout: Path, update: bool) -> int:
    """Start this helper for the running app. Returns its pid."""
    argv = [sys.executable, "-m", "agent_terminal.relaunch", "--checkout", str(checkout)]
    if update:
        argv.append("--update")
    return spawn_detached(argv, checkout)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="agent_terminal.relaunch")
    parser.add_argument("--checkout", type=Path, required=True)
    parser.add_argument("--update", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Wait, update, start the app again."""
    args = parse_args(sys.argv[1:] if argv is None else argv)
    settings = Settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        filename=settings.log_file,
        level=logging.INFO,
        format="%(asctime)s %(levelname)s relaunch: %(message)s",
    )
    if not wait_for_exit(settings.lock_file):
        log.error("the app did not exit in %.0fs; not starting another", EXIT_WAIT_SECONDS)
        return 1
    if args.update:
        before = head_commit(args.checkout)
        result = apply_update(args.checkout, settings.update_branch, settings.update_log_file)
        after = head_commit(args.checkout)
        detail = result.detail if not result.ok else f"{before} → {after}"
        record = {**asdict(result), "detail": detail, "log": str(settings.update_log_file)}
        settings.update_result_file.write_text(json.dumps(record), encoding="utf-8")
        log.info("update %s: %s", "done" if result.ok else "failed", detail)
    spawn_detached(relaunch_argv(sys.executable), args.checkout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
