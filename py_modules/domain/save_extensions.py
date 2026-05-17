"""Per-platform save file extension configuration.

Provides the list of save file extensions to look for when syncing saves.
The default covers RetroArch's standard .srm and .rtc extensions.
Platform-specific overrides can expand or replace this list.

Extension mapping based on RetroDECK core audit — see wiki/Save-File-Extensions.

No I/O, no service/adapter/lib imports. Pure functions only.
"""

from __future__ import annotations

_DEFAULT_EXTENSIONS: tuple[str, ...] = (".srm", ".rtc", ".sav")

# Platform-specific overrides. Keys are RomM platform slugs.
# Values completely replace the default list for that platform.
# See wiki/Save-File-Extensions for the research behind these mappings.
_PLATFORM_OVERRIDES: dict[str, tuple[str, ...]] = {
    "nds": (".srm", ".rtc", ".sav", ".dsv"),  # DeSmuME native format
    "segacd": (".srm", ".rtc", ".sav", ".brm"),  # Genesis Plus GX Sega CD BRAM
}


def get_save_extensions(platform_slug: str | None = None) -> tuple[str, ...]:
    """Return the save file extensions to search for a given platform.

    Parameters
    ----------
    platform_slug:
        RomM platform slug (e.g. "gba", "n64", "psx").
        If None or not in overrides, returns the default extensions.

    Returns
    -------
    tuple[str, ...]
        Tuple of file extensions including the leading dot (e.g. (".srm", ".rtc")).
    """
    if platform_slug is not None and platform_slug in _PLATFORM_OVERRIDES:
        return _PLATFORM_OVERRIDES[platform_slug]
    return _DEFAULT_EXTENSIONS
