from pathlib import Path

from grl.agent import install_skill, skill_destination
from grl.main import main


def test_codex_skill_uses_codex_home(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    destination = install_skill("codex")
    assert destination == tmp_path / "codex" / "skills" / "grl" / "SKILL.md"
    assert "name: grl" in destination.read_text()


def test_claude_skill_uses_claude_config_dir(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    assert skill_destination("claude") == tmp_path / "claude" / "skills" / "grl" / "SKILL.md"


def test_agent_setup_command(monkeypatch, tmp_path: Path, capsys):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    assert main(["agent", "setup", "--codex"]) == 0
    assert (tmp_path / "skills" / "grl" / "SKILL.md").is_file()
    assert "Installed GRL skill for codex" in capsys.readouterr().out


def test_version_command(capsys):
    import pytest

    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out == "grl 0.1.0\n"
