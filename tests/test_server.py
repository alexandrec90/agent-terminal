import asyncio
import json
import threading
from pathlib import Path
from urllib.parse import urlsplit

import aiohttp
import pytest

from agent_terminal.server import HOST, Backend, request_allowed
from agent_terminal.settings import Settings


@pytest.fixture
def backend(tmp_path: Path):
    settings = Settings(
        projects_root=tmp_path / "projects",
        data_dir=tmp_path / "data",
        claude_home=tmp_path / "claude",
        claude_config_file=tmp_path / "claude.json",
        codex_home=tmp_path / "codex",
        gh_command=str(tmp_path / "no-gh"),
    )
    (settings.projects_root / "demo").mkdir(parents=True)
    instance = Backend(settings)
    instance.start()
    yield instance
    instance.stop()


def test_a_slow_request_does_not_hold_up_the_messages_behind_it(backend):
    released = threading.Event()
    calls: list[str] = []

    async def slow(_message):
        calls.append("slow started")
        await asyncio.get_running_loop().run_in_executor(None, released.wait, 5)
        calls.append("slow finished")

    async def fast(_message):
        calls.append("input")

    backend._manager._handlers["pr_merge"] = slow
    backend._manager._handlers["input"] = fast

    async def body():
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(
                socket_url(backend), headers={"Origin": origin_of(backend)}
            ) as socket:
                await socket.send_str(json.dumps({"type": "pr_merge"}))
                await socket.send_str(json.dumps({"type": "input"}))
                for _ in range(200):
                    if "input" in calls:
                        break
                    await asyncio.sleep(0.01)
                assert "input" in calls
                assert "slow finished" not in calls
                released.set()

    asyncio.run(body())


def test_keystrokes_stay_in_order(backend):
    seen: list[str] = []

    async def record(message):
        await asyncio.sleep(0.01 if message["data"] == "a" else 0)
        seen.append(message["data"])

    backend._manager._handlers["input"] = record

    async def body():
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(
                socket_url(backend), headers={"Origin": origin_of(backend)}
            ) as socket:
                for data in ("a", "b", "c"):
                    await socket.send_str(json.dumps({"type": "input", "data": data}))
                for _ in range(200):
                    if len(seen) == 3:
                        break
                    await asyncio.sleep(0.01)

    asyncio.run(body())
    assert seen == ["a", "b", "c"]


def socket_url(backend: Backend, token: str | None = None) -> str:
    parts = urlsplit(backend.url)
    query = parts.query if token is None else f"token={token}"
    return f"ws://{parts.netloc}/ws?{query}"


def origin_of(backend: Backend) -> str:
    return f"http://{urlsplit(backend.url).netloc}"


@pytest.mark.parametrize(
    ("token", "origin", "allowed"),
    [
        ("right", f"http://{HOST}:5000", True),
        ("right", None, True),
        ("wrong", f"http://{HOST}:5000", False),
        ("", f"http://{HOST}:5000", False),
        ("right", "https://evil.example", False),
        ("right", f"http://{HOST}:5001", False),
        ("right", "null", False),
    ],
)
def test_request_allowed(token, origin, allowed):
    assert request_allowed("right", token, origin, 5000) is allowed


def test_two_runs_do_not_share_a_token(tmp_path):
    settings = Settings(data_dir=tmp_path)
    assert Backend(settings).url != Backend(settings).url


def test_the_page_and_its_scripts_are_served(backend):
    async def body():
        async with aiohttp.ClientSession() as session:
            for path in ("/", "/app.js", "/style.css", "/vendor/xterm.js"):
                async with session.get(origin_of(backend) + path) as response:
                    assert response.status == 200, path

    asyncio.run(body())


def test_a_wrong_token_is_refused(backend):
    async def body():
        async with aiohttp.ClientSession() as session:
            with pytest.raises(aiohttp.WSServerHandshakeError) as refused:
                await session.ws_connect(socket_url(backend, "wrong"))
            assert refused.value.status == 403

    asyncio.run(body())


def test_another_origin_is_refused_even_with_the_token(backend):
    async def body():
        async with aiohttp.ClientSession() as session:
            with pytest.raises(aiohttp.WSServerHandshakeError) as refused:
                await session.ws_connect(
                    socket_url(backend), headers={"Origin": "https://evil.example"}
                )
            assert refused.value.status == 403

    asyncio.run(body())


def test_a_page_says_hello_and_hears_the_projects(backend):
    async def body():
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(
                socket_url(backend), headers={"Origin": origin_of(backend)}
            ) as socket:
                await socket.send_str(json.dumps({"type": "hello", "rows": 24, "cols": 80}))
                first = json.loads((await socket.receive(timeout=5)).data)
                second = json.loads((await socket.receive(timeout=5)).data)
        assert first["type"] == "projects"
        assert [p["name"] for p in first["projects"]] == ["demo"]
        assert second == {"type": "state", "active_id": None, "tabs": [], "prs": []}

    asyncio.run(body())
