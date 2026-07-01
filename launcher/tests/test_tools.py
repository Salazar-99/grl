from grl.config import LaunchToolsConfig
from grl.tools import helm_spec, kubectl_spec, platform_key, terraform_spec


def test_platform_key_normalizes_arm64():
    system, machine = platform_key()
    assert system in {"darwin", "linux", "windows"}
    assert machine in {"amd64", "arm64"}


def test_tool_specs_have_urls():
    config = LaunchToolsConfig()
    for spec_fn in (terraform_spec, helm_spec, kubectl_spec):
        spec = spec_fn(config)
        assert spec.url.startswith("https://")
        assert spec.version
