"""Firmware path parsing and platform-to-firmware slug remapping.

Pure string logic for translating between RomM's firmware ``file_path``
layout and platform slugs. BIOS file I/O, registry loading, and HTTP
calls live in FirmwareService; anything that reaches the filesystem or
network does not belong here.
"""

from __future__ import annotations

_PLATFORM_TO_FIRMWARE_SLUGS: dict[str, list[str]] = {
    "psx": ["psx", "ps"],
    "ps2": ["ps2"],
}


def parse_firmware_slug(file_path: str) -> str:
    """Extract the firmware slug from a RomM firmware ``file_path``.

    Returns the slug directly under ``bios/`` (e.g. ``"bios/ps/scph.bin"``
    -> ``"ps"``), or the first segment for non-``bios`` layouts. Returns
    an empty string when ``file_path`` has fewer than two segments.
    """
    parts = file_path.strip("/").split("/")
    if len(parts) >= 2 and parts[0] == "bios":
        return parts[1]
    elif len(parts) >= 2:
        return parts[0]
    return ""


def resolve_firmware_slugs(platform_slug: str) -> list[str]:
    """Map a platform slug to the firmware directory slugs RomM may use.

    RomM uses different slugs for platforms vs firmware directories
    (e.g. platform ``"psx"`` is stored under firmware dir ``"ps"``).
    Unknown platforms fall through as ``[platform_slug]``.
    """
    return _PLATFORM_TO_FIRMWARE_SLUGS.get(platform_slug, [platform_slug])
