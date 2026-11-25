import asyncio

import pytest

from agent_terminal.pty_session import PtyProcess, PtySession
from agent_terminal.sessions import LocalSessions, Session, SessionSource, Survivor


def never_spawn(*_: object) -> PtyProcess:
    raise AssertionError("nothing starts here")


def test_local_sessions_are_pseudo_consoles_in_the_app():
    killed: list[int] = []
    source: SessionSource = LocalSessions(spawn=never_spawn, kill=killed.append)
    session: Session = source.new("t", print, print)
    assert isinstance(session, PtySession)
    assert source.status() == {"kept": False, "stale": False}


def test_nothing_survives_a_local_window():
    source = LocalSessions()

    async def body() -> None:
        assert await source.survivors() == {}
        await source.discard(Survivor(tab_id="t", key="k", alive=True))
        await source.detach()
        await source.shutdown()

    asyncio.run(body())
    with pytest.raises(LookupError):
        source.adopt(Survivor(tab_id="t", key="k", alive=True), print, print)


def test_a_survivor_defaults_to_nothing_held():
    survivor = Survivor(tab_id="t", key="k", alive=False, exit_code=2)
    assert (survivor.screen, survivor.unread, survivor.state, survivor.end) == ("", "", {}, 0)
