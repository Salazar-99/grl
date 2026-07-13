from pathlib import Path

from vms.kernel_config import REQUIRED_KERNEL_OPTIONS, validate_kernel_config


def test_kernel_config_requires_bootstrap_filesystems(tmp_path: Path) -> None:
    config = tmp_path / "config"
    config.write_text(
        "\n".join(f"{option}=y" for option in REQUIRED_KERNEL_OPTIONS) + "\n"
    )
    validate_kernel_config(config)

    config.write_text("CONFIG_BLK_DEV_INITRD=y\n")
    try:
        validate_kernel_config(config)
    except ValueError as error:
        assert "CONFIG_SQUASHFS" in str(error)
    else:
        raise AssertionError("incomplete kernel config was accepted")
