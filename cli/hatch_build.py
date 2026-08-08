"""Bundle the launcher assets needed by an installed ``grl`` CLI wheel."""

from __future__ import annotations

from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class CustomBuildHook(BuildHookInterface):
    """Add Terraform roots and Helm charts from the repository to the wheel.

    These assets intentionally live outside the Python package in the checkout.
    Enumerating files rather than force-including the directories avoids shipping
    local Terraform providers, state, and module caches.
    """

    PLUGIN_NAME = "custom"

    def initialize(self, version: str, build_data: dict) -> None:
        repo_root = Path(self.root).parent
        assets = (
            (repo_root / "infra" / "aws", Path("grl/data/infra/aws")),
            (repo_root / "infra" / "byok", Path("grl/data/infra/byok")),
            (
                repo_root / "infra" / "modules" / "resources" / "chart",
                Path("grl/data/chart"),
            ),
            (
                repo_root / "infra" / "charts" / "environments",
                Path("grl/data/environments-chart"),
            ),
        )
        force_include = build_data.setdefault("force_include", {})
        for source_root, destination_root in assets:
            for source in source_root.rglob("*"):
                if not source.is_file() or ".terraform" in source.parts:
                    continue
                if source.name.startswith("terraform.tfstate"):
                    continue
                force_include[str(source)] = str(destination_root / source.relative_to(source_root))
