"""Per-system save file extension configuration.

Provides the list of save file extensions to look for when syncing saves.
The default covers RetroArch's standard .srm and .rtc extensions.
Per-system overrides can expand or replace this list. Override keys are
RetroDECK *system* names (the normalized value from resolve_system /
platform_map), NOT raw RomM platform slugs — keying by system keeps the
extension lookup aligned with the rest of the local flow (save directory,
cores, gamelists), which is also system-keyed.

Extension mapping based on RetroDECK core audit — see wiki/Save-File-Extensions.

No I/O, no service/adapter/lib imports. Pure functions only.
"""

from __future__ import annotations

_DEFAULT_EXTENSIONS: tuple[str, ...] = (".srm", ".rtc", ".sav")

# Platform-specific overrides. Keys are RetroDECK *system* names — the
# normalized value produced by resolve_system / platform_map, NOT raw RomM
# platform slugs. Values completely replace the default list for that system.
# See wiki/Save-File-Extensions for the research behind these mappings.
# CDTV (.nvr) is deferred: RomM's slug ``commodore-cdtv`` is not yet in
# platform_map, so no install can reach a ``cdtv`` system today — re-add a
# ``cdtv`` key once platform_map maps ``commodore-cdtv -> cdtv`` (tracked
# separately).
_PLATFORM_OVERRIDES: dict[str, tuple[str, ...]] = {
    "nds": (".srm", ".rtc", ".sav", ".dsv"),  # DeSmuME native format
    "segacd": (".srm", ".rtc", ".sav", ".brm"),  # Genesis Plus GX Sega CD BRAM
    "saturn": (".srm", ".rtc", ".sav", ".bkr", ".bcr", ".smpc"),  # Beetle Saturn / yabasanshiro backup RAM
    "ngp": (".srm", ".rtc", ".sav", ".flash", ".ngf"),  # Beetle NeoPop (.flash) / RACE (.ngf)
    "ngpc": (".srm", ".rtc", ".sav", ".flash", ".ngf"),
    "pokemini": (".srm", ".rtc", ".sav", ".eep"),  # PokeMini EEPROM
    "amiga": (".srm", ".rtc", ".sav", ".nvr"),  # PUAE non-volatile RAM
    "amigacd32": (".srm", ".rtc", ".sav", ".nvr"),
}


def get_save_extensions(system: str | None = None) -> tuple[str, ...]:
    """Return the save file extensions to search for a given system.

    Parameters
    ----------
    system:
        RetroDECK *system* name — the normalized value from
        resolve_system / platform_map (e.g. "gba", "saturn", "amiga"), NOT
        the raw RomM platform slug. If None or not in overrides, returns the
        default extensions.

    Returns
    -------
    tuple[str, ...]
        Tuple of file extensions including the leading dot (e.g. (".srm", ".rtc")).
    """
    if system is not None and system in _PLATFORM_OVERRIDES:
        return _PLATFORM_OVERRIDES[system]
    return _DEFAULT_EXTENSIONS
