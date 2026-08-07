"""Install the bundled GRL operating skill for supported coding agents."""

from __future__ import annotations

import os
from importlib import resources
from pathlib import Path


def skill_source() -> str:
    return (resources.files("grl") / "data" / "grl-skill.md").read_text()


def skill_destination(agent: str) -> Path:
    if agent == "codex":
        base = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    elif agent == "claude":
        base = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude"))
    else:  # Defensive: argparse only permits known agents.
        raise ValueError(f"unsupported agent: {agent}")
    return base / "skills" / "grl" / "SKILL.md"


def install_skill(agent: str) -> Path:
    destination = skill_destination(agent)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(skill_source())
    return destination
