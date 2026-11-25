"""What the app and the session host say to each other, and how each finds the other.

One JSON object per line over a loopback socket. The first line from the app is `hello`
with the token from the address file; the host answers `welcome`, listing what it holds.
A request that wants an answer carries `req`, and the answer is a `reply` with the same.
Unasked, the host sends `output` and `exit`.

**`PROTOCOL` changes only with a change the other side cannot read.** A host is kept
across app updates, so an app must talk to the host the last version started. When the
number differs the app asks the old host to `shutdown`, which is why that one message
must keep its meaning in every version, and starts its own.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

PROTOCOL = 1
# A replayed screen goes in one line, every escape of it spelled `\u001b`.
LINE_LIMIT = 64 * 1024 * 1024
HOST = "127.0.0.1"

PACKAGE_DIR = Path(__file__).resolve().parent
# Everything the host imports of the app. Their hash is the host's build: an update that
# changes none of them leaves the running host as good as a new one.
HOST_MODULES = ("host.py", "host_protocol.py", "pty_session.py", "instance.py")


def encode(message: dict[str, Any]) -> bytes:
    """One message, as the line that carries it."""
    return json.dumps(message, separators=(",", ":")).encode("utf-8") + b"\n"


def decode(line: bytes) -> dict[str, Any]:
    """One line's message. Raises `ValueError` for anything that is not one."""
    message = json.loads(line)
    if not isinstance(message, dict):
        raise ValueError("a message is a JSON object")
    return message


def host_build(package_dir: Path = PACKAGE_DIR) -> str:
    """A short hash of the host's code as it is on disk now."""
    digest = hashlib.sha256()
    for name in HOST_MODULES:
        digest.update(name.encode())
        digest.update((package_dir / name).read_bytes())
    return digest.hexdigest()[:12]


@dataclass(frozen=True)
class HostAddress:
    """Where a running host listens, and the token it takes. The file holding it is in the
    user's own data directory, which is all that keeps another user from reading it."""

    port: int
    token: str
    pid: int
    protocol: int
    build: str


def write_address(path: Path, address: HostAddress) -> None:
    """Publish the address, atomically: a reader never sees half of it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(asdict(address)), encoding="utf-8")
    os.replace(temporary, path)


def read_address(path: Path) -> HostAddress | None:
    """The published address, or None if there is none or it cannot be read."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return HostAddress(
            port=int(data["port"]),
            token=str(data["token"]),
            pid=int(data["pid"]),
            protocol=int(data["protocol"]),
            build=str(data["build"]),
        )
    except (OSError, ValueError, KeyError, TypeError):
        return None


def remove_address(path: Path, pid: int) -> None:
    """Take the address down, unless a newer host has already replaced it."""
    current = read_address(path)
    if current is not None and current.pid == pid:
        with contextlib.suppress(OSError):
            path.unlink()
