"""Validate kernel capabilities required by the external bootstrap."""

from pathlib import Path

REQUIRED_KERNEL_OPTIONS = (
    "CONFIG_BLK_DEV_INITRD",
    "CONFIG_RD_GZIP",
    "CONFIG_SQUASHFS",
    "CONFIG_OVERLAY_FS",
    "CONFIG_DEVTMPFS",
    "CONFIG_EXT4_FS",
    "CONFIG_VIRTIO_BLK",
    "CONFIG_VIRTIO_MMIO",
    "CONFIG_UNIX98_PTYS",
    "CONFIG_DEVPTS_MULTIPLE_INSTANCES",
)


def validate_kernel_config(path: Path) -> None:
    values: dict[str, str] = {}
    for line in path.read_text().splitlines():
        if "=" in line and line.startswith("CONFIG_"):
            name, value = line.split("=", 1)
            values[name] = value
    missing = [name for name in REQUIRED_KERNEL_OPTIONS if values.get(name) != "y"]
    if missing:
        raise ValueError(
            "kernel lacks required built-in bootstrap support: " + ", ".join(missing)
        )
