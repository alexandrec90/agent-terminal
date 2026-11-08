import json
import subprocess

from agent_terminal.settings import Settings
from agent_terminal.trust import claude_trusted, codex_trusted, is_trusted


def write_claude(path, entries):
    path.write_text(json.dumps({"projects": entries}), encoding="utf-8")


def write_codex(home, path, level="trusted"):
    escaped = str(path).lower().replace("\\", "\\\\")
    (home / "config.toml").write_text(
        f'[projects."{escaped}"]\ntrust_level = "{level}"\n', encoding="utf-8"
    )


class TestClaude:
    def test_an_accepted_folder(self, tmp_path):
        config = tmp_path / "claude.json"
        write_claude(config, {tmp_path.as_posix(): {"hasTrustDialogAccepted": True}})
        assert claude_trusted(config, tmp_path)

    def test_trust_covers_subfolders(self, tmp_path):
        config = tmp_path / "claude.json"
        write_claude(config, {tmp_path.as_posix(): {"hasTrustDialogAccepted": True}})
        assert claude_trusted(config, tmp_path / "a" / "b")

    def test_a_subfolders_trust_does_not_cover_its_parent(self, tmp_path):
        config = tmp_path / "claude.json"
        write_claude(config, {(tmp_path / "a").as_posix(): {"hasTrustDialogAccepted": True}})
        assert not claude_trusted(config, tmp_path)

    def test_a_refused_folder(self, tmp_path):
        config = tmp_path / "claude.json"
        write_claude(config, {tmp_path.as_posix(): {"hasTrustDialogAccepted": False}})
        assert not claude_trusted(config, tmp_path)

    def test_an_unlisted_folder(self, tmp_path):
        config = tmp_path / "claude.json"
        write_claude(config, {})
        assert not claude_trusted(config, tmp_path)

    def test_no_config_file(self, tmp_path):
        assert not claude_trusted(tmp_path / "absent.json", tmp_path)

    def test_a_corrupt_config_file(self, tmp_path):
        config = tmp_path / "claude.json"
        config.write_text("{not json", encoding="utf-8")
        assert not claude_trusted(config, tmp_path)

    def test_case_and_slashes_do_not_matter(self, tmp_path):
        config = tmp_path / "claude.json"
        write_claude(config, {str(tmp_path).upper(): {"hasTrustDialogAccepted": True}})
        assert claude_trusted(config, tmp_path)


class TestCodex:
    def test_a_trusted_folder(self, tmp_path):
        write_codex(tmp_path, tmp_path)
        assert codex_trusted(tmp_path, tmp_path)

    def test_any_other_level_is_not_trust(self, tmp_path):
        write_codex(tmp_path, tmp_path, level="untrusted")
        assert not codex_trusted(tmp_path, tmp_path)

    def test_no_config_file(self, tmp_path):
        assert not codex_trusted(tmp_path, tmp_path)

    def test_a_corrupt_config_file(self, tmp_path):
        (tmp_path / "config.toml").write_text("[[[", encoding="utf-8")
        assert not codex_trusted(tmp_path, tmp_path)

    def test_trusting_the_repository_covers_a_folder_in_it(self, tmp_path):
        repository = tmp_path / "repo"
        below = repository / "sub"
        below.mkdir(parents=True)
        subprocess.run(["git", "init"], cwd=repository, check=True, capture_output=True)  # noqa: S607 - a fixed test command
        write_codex(tmp_path, repository)
        assert codex_trusted(tmp_path, below)


def test_is_trusted_asks_the_chosen_agent(tmp_path):
    config = tmp_path / "claude.json"
    write_claude(config, {tmp_path.as_posix(): {"hasTrustDialogAccepted": True}})
    settings = Settings(claude_config_file=config, codex_home=tmp_path)
    assert is_trusted(settings, "claude", tmp_path)
    assert not is_trusted(settings, "codex", tmp_path)
