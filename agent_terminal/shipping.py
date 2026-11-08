"""Where a worktree stands in the shipping process, from what the process leaves in it.

A session ships by writing `logs/ship-intent.md` and stopping. The fix pass (devkit's
`scripts/ship_intent.py`, which this reads after and does not import) reaps that file: it
commits, pushes, opens the PR, moves the file to `ship-intent.shipped.md`, and records
the outcome in `ship-state.json`. So the file exists only between the two, and "unreaped"
needs no more than looking for it.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

INTENT_FILE = Path("logs") / "ship-intent.md"
STATE_FILE = Path("logs") / "ship-state.json"

NOT_SHIPPING = ""
INTENT = "intent"
REFUSED = "refused"

# A pass that tried this very intent and got no PR: `refused` is the commit stage saying
# no, `failed` a push or PR that did not go through.
STOPPED_STAGES = frozenset({"refused", "failed"})


def parse_intent(text: str) -> tuple[str, str]:
    """`(subject, body)`: the pass's own reading of the file, first line and the rest."""
    lines = text.strip().splitlines()
    if not lines:
        return "", ""
    return lines[0].strip().lstrip("#").strip(), "\n".join(lines[1:]).strip()


def intent_digest(subject: str, body: str) -> str:
    """What the state file remembers an intent as: the pass's `Intent.digest`."""
    return hashlib.sha256(f"{subject}\n{body}".encode()).hexdigest()[:12]


def _read_state(tree: Path) -> dict[str, object]:
    try:
        loaded = json.loads((tree / STATE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def ship_stage(tree: Path) -> str:
    """`INTENT` for an intent the pass has yet to reap, `REFUSED` for one it tried and
    could not ship, `NOT_SHIPPING` otherwise, as the pass itself would read the file: a
    subjectless file is nothing, and words already shipped from this tree are spent."""
    try:
        text = (tree / INTENT_FILE).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return NOT_SHIPPING
    subject, body = parse_intent(text)
    if not subject:
        return NOT_SHIPPING
    state = _read_state(tree)
    if state.get("intent") != intent_digest(subject, body):
        return INTENT
    if state.get("stage") == "shipped":
        return NOT_SHIPPING
    return REFUSED if state.get("stage") in STOPPED_STAGES else INTENT
