from pathlib import Path

from grl.config import GRLConfig
from grl.main import main


def test_launch_deployment_type_flag_overrides_config(monkeypatch, tmp_path: Path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("model: org/model\n")
    config = GRLConfig.model_validate({"model": "org/model"})
    captured: dict[str, object] = {}
    monkeypatch.setattr("grl.main.load_config", lambda _path: config)
    monkeypatch.setattr(
        "grl.main.launch", lambda resolved, **kwargs: captured.update(config=resolved)
    )

    assert main(["launch", str(config_path), "--deployment-type", "envs"]) == 0
    assert captured["config"].launch.deployment_type == "ENVS"
