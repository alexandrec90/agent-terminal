import pytest

from agent_terminal.host_protocol import (
    HOST_MODULES,
    PACKAGE_DIR,
    HostAddress,
    decode,
    encode,
    host_build,
    read_address,
    remove_address,
    write_address,
)


def test_a_message_is_one_line_and_comes_back_whole():
    message = {"type": "output", "data": "a\nb\x1b[0mé"}
    line = encode(message)
    assert line.endswith(b"\n")
    assert line.count(b"\n") == 1
    assert decode(line) == message


@pytest.mark.parametrize("line", [b"[1, 2]\n", b"not json\n", b"\n"])
def test_anything_but_an_object_is_refused(line):
    with pytest.raises(ValueError):
        decode(line)


def test_every_host_module_exists():
    for name in HOST_MODULES:
        assert (PACKAGE_DIR / name).is_file(), name


def test_the_build_follows_the_host_code(tmp_path):
    for name in HOST_MODULES:
        (tmp_path / name).write_text(f"# {name}\n")
    before = host_build(tmp_path)
    assert host_build(tmp_path) == before
    (tmp_path / HOST_MODULES[-1]).write_text("# changed\n")
    assert host_build(tmp_path) != before


def make_address(pid: int = 7) -> HostAddress:
    return HostAddress(4000, "t", pid, 1, "abc")


def test_an_address_round_trips(tmp_path):
    path = tmp_path / "data" / "host.json"
    write_address(path, make_address())
    assert read_address(path) == make_address()


@pytest.mark.parametrize("text", ["", "{}", '{"port": "x"}', "[]"])
def test_an_unreadable_address_is_none(tmp_path, text):
    path = tmp_path / "host.json"
    path.write_text(text)
    assert read_address(path) is None


def test_a_missing_address_is_none(tmp_path):
    assert read_address(tmp_path / "host.json") is None


def test_remove_leaves_a_newer_hosts_address(tmp_path):
    path = tmp_path / "host.json"
    write_address(path, make_address(pid=8))
    remove_address(path, pid=7)
    assert read_address(path) == make_address(pid=8)
    remove_address(path, pid=8)
    assert not path.exists()
